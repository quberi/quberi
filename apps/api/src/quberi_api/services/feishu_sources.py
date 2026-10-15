"""Rules for connecting a Feishu folder to a space.

Several organisations share one Feishu app, and a folder token is not a secret
(it appears in every share link). So holding a token must not be enough to pull
a folder into a space. Connecting is allowed only when all of these hold:

* the token is well-formed and the target space belongs to the caller's
  organisation, where the caller may add documents (editor or higher);
* the person connecting can open the folder in Feishu with *their own*
  authorisation, not just the shared app;
* no other organisation has connected the same folder first.

This module holds those rules. Registering and removing the source itself
stays in :mod:`quberi_api.services.document_sources`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.documents.models import DocumentSource, FeishuFolderClaim
from quberi_api.domain.integrations.models import Integration
from quberi_api.services.errors import (
    AlreadyExistsError,
    InvalidInputError,
    NotFoundError,
    ServiceError,
)

# Feishu folder tokens are short alphanumeric strings; this also keeps anything
# odd (paths, URLs, whitespace) out of the claim table.
_FOLDER_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


class FeishuAuthRequiredError(ServiceError):
    """The person has not authorised Quberi to use their Feishu account."""


class FeishuFolderAccessError(ServiceError):
    """Feishu will not let this person open the folder."""


def parse_folder_token(config: dict | None) -> str:
    """The folder token from a source config, validated."""

    raw = (config or {}).get("folder_token")
    token = raw.strip() if isinstance(raw, str) else ""
    if not _FOLDER_TOKEN_RE.match(token):
        raise InvalidInputError(
            "飞书文件夹标识无效，请从文件夹列表中选择。 "
            "Invalid Feishu folder; please pick it from the folder list."
        )
    return token


def parse_target_space_id(config: dict | None) -> uuid.UUID:
    raw = (config or {}).get("target_space_id")
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        raise InvalidInputError(
            "需要指定目标空间。 A target space is required."
        ) from None


async def prepare_config(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    config: dict | None,
) -> tuple[dict, str]:
    """Validate a Feishu source config and return ``(clean_config, folder_token)``.

    Only the two known keys survive, so a caller cannot smuggle other fields
    into the stored config. The target space must be in the organisation and
    the actor must be allowed to add documents to it.
    """

    from quberi_api.services.documents.core import caller_has_space_role

    folder_token = parse_folder_token(config)
    space_id = parse_target_space_id(config)
    allowed = await caller_has_space_role(
        session,
        user_id=actor_user_id,
        organization_id=organization_id,
        space_id=space_id,
        required="editor",
    )
    if not allowed:
        # Same answer whether the space is missing, foreign, or merely not
        # writable by this person, so it does not reveal which spaces exist.
        raise NotFoundError(
            "目标空间不存在，或你没有在其中添加文档的权限。 "
            "The target space does not exist, or you cannot add documents to it."
        )
    return {"folder_token": folder_token, "target_space_id": str(space_id)}, folder_token


async def claim_folder(
    session: AsyncSession, *, organization_id: uuid.UUID, folder_token: str
) -> None:
    """Record this organisation as the folder's owner, or refuse.

    Must run inside the transaction that creates the source, so a failed
    registration leaves no claim behind. ``ON CONFLICT DO NOTHING`` on the
    primary key decides races: exactly one organisation inserts the row.
    """

    await session.execute(
        pg_insert(FeishuFolderClaim)
        .values(folder_token=folder_token, organization_id=organization_id)
        .on_conflict_do_nothing(index_elements=[FeishuFolderClaim.folder_token])
    )
    owner = (
        await session.execute(
            select(FeishuFolderClaim.organization_id).where(
                FeishuFolderClaim.folder_token == folder_token
            )
        )
    ).scalar_one()
    if owner != organization_id:
        raise AlreadyExistsError(
            "这个飞书文件夹已被其他组织连接。 "
            "This Feishu folder is already connected by another organization."
        )


async def release_folder_if_unused(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    folder_token: str,
    excluding_source_id: uuid.UUID,
) -> None:
    """Drop the claim when no other Feishu source in the org uses the folder."""

    still_used = (
        await session.execute(
            select(DocumentSource.id).where(
                DocumentSource.organization_id == organization_id,
                DocumentSource.kind == "feishu",
                DocumentSource.id != excluding_source_id,
                DocumentSource.config["folder_token"].astext == folder_token,
            ).limit(1)
        )
    ).first()
    if still_used is None:
        await session.execute(
            delete(FeishuFolderClaim).where(
                FeishuFolderClaim.folder_token == folder_token,
                FeishuFolderClaim.organization_id == organization_id,
            )
        )


async def org_has_claim(
    session: AsyncSession, *, organization_id: uuid.UUID, folder_token: str
) -> bool:
    row = (
        await session.execute(
            select(FeishuFolderClaim.folder_token).where(
                FeishuFolderClaim.folder_token == folder_token,
                FeishuFolderClaim.organization_id == organization_id,
            )
        )
    ).first()
    return row is not None


UserTokenProvider = Callable[[AsyncSession, uuid.UUID, uuid.UUID], Awaitable[Optional[str]]]
FolderOpener = Callable[..., Awaitable[None]]


async def verify_user_can_open_folder(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    folder_token: str,
    base_url: str,
    get_user_token: Optional[UserTokenProvider] = None,
    open_folder: Optional[FolderOpener] = None,
) -> None:
    """Refuse unless the person can open the folder with their own Feishu login.

    ``get_user_token`` and ``open_folder`` default to the real Feishu calls and
    exist so the rule can be tested without a network.
    """

    from quberi_api.services.integrations.feishu.client import FeishuError
    from quberi_api.services.integrations.feishu.docs import user_can_open_folder
    from quberi_api.services.integrations.feishu.oauth import get_valid_user_token

    integration_id = (
        await session.execute(
            select(Integration.id).where(
                Integration.organization_id == organization_id,
                Integration.provider == "feishu",
            )
        )
    ).scalar_one_or_none()
    token: str | None = None
    if integration_id is not None:
        if get_user_token is not None:
            token = await get_user_token(session, integration_id, user_id)
        else:
            token = await get_valid_user_token(
                session, integration_id=integration_id, user_id=user_id
            )
    # Close the read transaction before the network call below.
    await session.commit()
    if not token:
        raise FeishuAuthRequiredError(
            "请先连接你的飞书账号。 Please connect your Feishu account first."
        )
    opener = open_folder or user_can_open_folder
    try:
        await opener(
            base_url=base_url, user_access_token=token, folder_token=folder_token
        )
    except FeishuError as exc:
        raise FeishuFolderAccessError(
            "你的飞书账号无法访问这个文件夹。 "
            "Your Feishu account cannot open this folder."
        ) from exc
