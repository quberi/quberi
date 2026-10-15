"""Registered document sources — CRUD, test, sync dispatch.

The ``local`` source is auto-provisioned per organisation the first time
a document is uploaded (see ``ensure_local_source``). Feishu / OSS / S3
sources must be explicitly registered by an admin.

Credentials storage
-------------------
For Slice E the ``credentials_encrypted`` column holds a base64-wrapped
JSON blob — NOT truly encrypted. This is a deliberate TODO tagged
throughout: when a real external connector lands we replace the
``_seal`` / ``_unseal`` helpers with a Fernet-based implementation
without touching the API contract.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import DocumentSource
from quberi_api.domain.enums import (
    DOCUMENT_CACHING_POLICY,
    DOCUMENT_SOURCE_KINDS,
)
from quberi_api.services import feishu_sources
from quberi_api.services.documents.connectors import (
    feishu,
    oss_aliyun,
    s3_aws,
)
from quberi_api.services.errors import (
    AlreadyExistsError,
    InvalidInputError,
    NotFoundError,
    ServiceError,
)

_KINDS: frozenset[str] = frozenset(DOCUMENT_SOURCE_KINDS)
_POLICIES: frozenset[str] = frozenset(DOCUMENT_CACHING_POLICY)

# The connector modules for kinds that need dispatch (local is handled
# in-line inside ``services.documents.core``, so it's not here).
_EXTERNAL_CONNECTORS = {
    "feishu": feishu,
    "oss_aliyun": oss_aliyun,
    "s3_aws": s3_aws,
}


# ---------------------------------------------------------------------------
# Result payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentSourceView:
    source: DocumentSource
    # The service intentionally does NOT expose credentials on reads —
    # ``credentials_present`` tells the UI whether they were ever set,
    # nothing more.
    credentials_present: bool


def _to_view(row: DocumentSource) -> DocumentSourceView:
    return DocumentSourceView(
        source=row,
        credentials_present=row.credentials_encrypted is not None,
    )


# ---------------------------------------------------------------------------
# Credentials sealing (placeholder — real Fernet in a follow-up slice)
# ---------------------------------------------------------------------------


def _seal(credentials: dict | None) -> Optional[str]:
    if not credentials:
        return None
    payload = json.dumps(credentials, ensure_ascii=False).encode()
    # TODO(slice-E.1): replace with Fernet(settings.storage_crypt_key).encrypt
    return "b64:" + base64.b64encode(payload).decode()


def _unseal(ciphertext: Optional[str]) -> dict:
    if not ciphertext:
        return {}
    if ciphertext.startswith("b64:"):
        raw = base64.b64decode(ciphertext[4:])
        return json.loads(raw.decode())
    raise ServiceError("unknown credentials envelope")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_kind(kind: str) -> str:
    if kind not in _KINDS:
        raise InvalidInputError(
            f"unknown source kind {kind!r} (allowed: {sorted(_KINDS)})"
        )
    return kind


def _validate_policy(policy: str) -> str:
    if policy not in _POLICIES:
        raise InvalidInputError(
            f"unknown caching policy {policy!r} (allowed: {sorted(_POLICIES)})"
        )
    return policy


def _validate_name(name: str) -> str:
    trimmed = name.strip()
    if not trimmed:
        raise InvalidInputError("source name is required")
    if len(trimmed) > 200:
        raise InvalidInputError("source name exceeds 200 characters")
    return trimmed


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_sources(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
) -> list[DocumentSourceView]:
    stmt = (
        select(DocumentSource)
        .where(DocumentSource.organization_id == organization_id)
        .order_by(DocumentSource.created_at.asc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_to_view(r) for r in rows]


async def get_source(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> DocumentSourceView:
    stmt = select(DocumentSource).where(
        DocumentSource.id == source_id,
        DocumentSource.organization_id == organization_id,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"document source not found: {source_id}")
    return _to_view(row)


# ---------------------------------------------------------------------------
# The "local" source is a first-class citizen; every org gets exactly one.
# It's created lazily so we don't need a data migration.
# ---------------------------------------------------------------------------


DEFAULT_LOCAL_SOURCE_NAME = "Local uploads"


async def ensure_local_source(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
) -> DocumentSource:
    """Return the org's local source, creating it on first call.

    Safe under contention — the ``(organization_id, kind, name)`` unique
    constraint plus a defensive re-fetch on ``IntegrityError`` gives us
    idempotent semantics.
    """

    stmt = select(DocumentSource).where(
        DocumentSource.organization_id == organization_id,
        DocumentSource.kind == "local",
        DocumentSource.name == DEFAULT_LOCAL_SOURCE_NAME,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is not None:
        return row

    row = DocumentSource(
        organization_id=organization_id,
        kind="local",
        name=DEFAULT_LOCAL_SOURCE_NAME,
        config={"description": "Direct uploads through the Quberi UI."},
        credentials_encrypted=None,
        caching_policy="mirror",
        status="active",
        created_by_user_id=None,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        row = (await session.execute(stmt)).scalar_one()
    return row


# ---------------------------------------------------------------------------
# Register an external source (Feishu / OSS / S3)
# ---------------------------------------------------------------------------


async def _free_source_name(
    session: AsyncSession, *, organization_id: uuid.UUID, kind: str, base: str
) -> str:
    """``base``, or ``base (2)``, ``base (3)`` ... the first one not yet used."""

    taken = set(
        (
            await session.execute(
                select(DocumentSource.name).where(
                    DocumentSource.organization_id == organization_id,
                    DocumentSource.kind == kind,
                    DocumentSource.name.like(f"{base}%"),
                )
            )
        )
        .scalars()
        .all()
    )
    if base not in taken:
        return base
    n = 2
    while True:
        suffix = f" ({n})"
        candidate = f"{base[: 200 - len(suffix)]}{suffix}"
        if candidate not in taken:
            return candidate
        n += 1


async def register_source(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    kind: str,
    name: str,
    config: dict,
    credentials: dict | None,
    caching_policy: str = "mirror",
) -> DocumentSourceView:
    kind = _validate_kind(kind)
    if kind == "local":
        raise InvalidInputError(
            "local sources are managed automatically; do not register them"
        )
    name = _validate_name(name)
    caching_policy = _validate_policy(caching_policy)
    folder_token: str | None = None
    if kind == "feishu":
        # Keep only the two known keys, and check the target space and the
        # actor's right to add documents to it, before anything is written.
        config, folder_token = await feishu_sources.prepare_config(
            session,
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            config=config,
        )
        # Close the read transaction so the write transaction below can begin.
        await session.commit()

    async with session.begin():
        if folder_token is not None:
            await feishu_sources.claim_folder(
                session, organization_id=organization_id, folder_token=folder_token
            )
            # The UI names Feishu connections after the folder, so the same
            # folder connected to a second space would collide: number it.
            name = await _free_source_name(
                session, organization_id=organization_id, kind=kind, base=name
            )
        row = DocumentSource(
            organization_id=organization_id,
            kind=kind,
            name=name,
            config=config or {},
            credentials_encrypted=_seal(credentials),
            caching_policy=caching_policy,
            status="active",
            created_by_user_id=actor_user_id,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise AlreadyExistsError(
                f"a {kind} source named {name!r} already exists in this organisation"
            ) from exc

        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="document_source.registered",
                resource_type="document_source",
                resource_id=row.id,
                payload={
                    "kind": kind,
                    "name": name,
                    **(
                        {
                            "folder_token": folder_token,
                            "target_space_id": (config or {}).get("target_space_id"),
                        }
                        if folder_token is not None
                        else {}
                    ),
                },
            )
        )
    return _to_view(row)


async def delete_source(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> None:
    async with session.begin():
        stmt = select(DocumentSource).where(
            DocumentSource.id == source_id,
            DocumentSource.organization_id == organization_id,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"document source not found: {source_id}")
        if row.kind == "local":
            raise InvalidInputError(
                "the local source cannot be removed; delete individual documents instead"
            )
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="document_source.deleted",
                resource_type="document_source",
                resource_id=row.id,
                payload={"kind": row.kind, "name": row.name},
            )
        )
        claimed_token = (
            (row.config or {}).get("folder_token") if row.kind == "feishu" else None
        )
        row_id = row.id
        await session.delete(row)
        if isinstance(claimed_token, str) and claimed_token:
            await session.flush()
            await feishu_sources.release_folder_if_unused(
                session,
                organization_id=organization_id,
                folder_token=claimed_token,
                excluding_source_id=row_id,
            )


# ---------------------------------------------------------------------------
# Connect / test / sync dispatch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectorResult:
    ok: bool
    message: str
    details: dict


async def test_source(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> ConnectorResult:
    view = await get_source(
        session, source_id=source_id, organization_id=organization_id
    )
    row = view.source
    if row.kind == "local":
        return ConnectorResult(
            ok=True,
            message="local source is always available",
            details={},
        )
    connector = _EXTERNAL_CONNECTORS.get(row.kind)
    if connector is None:
        raise InvalidInputError(f"no connector registered for kind {row.kind!r}")
    try:
        await connector.test_connection(row.config, _unseal(row.credentials_encrypted))
        return ConnectorResult(ok=True, message="connection ok", details={})
    except ServiceError as exc:
        return ConnectorResult(ok=False, message=str(exc), details={})


async def sync_source(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> ConnectorResult:
    view = await get_source(
        session, source_id=source_id, organization_id=organization_id
    )
    row = view.source
    if row.kind == "local":
        return ConnectorResult(
            ok=True,
            message="local source has no sync — documents arrive via direct upload",
            details={},
        )
    connector = _EXTERNAL_CONNECTORS.get(row.kind)
    if connector is None:
        raise InvalidInputError(f"no connector registered for kind {row.kind!r}")

    # The get_source() call above executed a SELECT which auto-began a
    # transaction; commit it so the audit-write below can open its own.
    await session.commit()
    async with session.begin():
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="document_source.sync_attempted",
                resource_type="document_source",
                resource_id=row.id,
                payload={"kind": row.kind, "name": row.name},
            )
        )
    # Pass through the resolved org / actor to the connector via
    # config-scoped side channels. The connector protocol was defined
    # before we needed these, and threading them through the positional
    # signature would break OSS / S3 stubs. Injecting into ``config``
    # is a mild abuse but self-contained; the real interface upgrade
    # lands with the ``integrations`` table refactor.
    injected_config = {
        **(row.config or {}),
        "organization_id": str(organization_id),
        "_source_id": str(row.id),
        "_actor_user_id": str(actor_user_id),
    }
    try:
        await connector.sync(
            injected_config, _unseal(row.credentials_encrypted)
        )  # stubs raise
    except ServiceError as exc:
        return ConnectorResult(ok=False, message=str(exc), details={})

    # Stamp ``last_synced_at`` in its own committed transaction. The
    # connector ran against a *separate* session/engine, so ``row`` here
    # is stale — re-fetch it fresh before the write. A bare ``flush()``
    # without commit was the earlier bug: the UI kept showing "never
    # synced" because the update was rolled back on session close.
    async with session.begin():
        fresh = (
            await session.execute(
                select(DocumentSource).where(DocumentSource.id == row.id)
            )
        ).scalar_one()
        fresh.last_synced_at = datetime.utcnow()
    return ConnectorResult(ok=True, message="synced", details={})
