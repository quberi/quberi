"""Platform-admin management of the outside tools allowed to ask for access.

Which tools may start a login, and where each may be sent its authorisation code, is
configuration, not code: a platform admin adds ChatGPT or the hosted Claude apps here, or
switches a tool off, without a release.

Endpoints
---------
GET    /admin/oauth-clients        - every configured tool, and the latest attempts still refused
POST   /admin/oauth-clients        - allow a new tool
PATCH  /admin/oauth-clients/{id}   - edit, or switch on/off
DELETE /admin/oauth-clients/{id}   - remove

Switching a tool off (or removing it) stops new logins, its refresh tokens, and its access to the
MCP server at once. Its access to the REST API ends when the access token expires, minutes at
most.

This decides where authorisation codes may be delivered, so every change is written to the
API log, and the rules are checked: an https address must name a real domain, a loopback rule
is the single word ``loopback``, and nothing may contain a wildcard.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session, get_infrastructure
from quberi_api.api.security import require_platform_admin
from quberi_api.domain.oauth.models import OAuthClientConfig
from quberi_api.infrastructure.registry import Infrastructure
from quberi_api.services.oauth import clients

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/oauth-clients",
    tags=["admin"],
    dependencies=[Depends(require_platform_admin)],
)


class OAuthClientView(BaseModel):
    id: uuid.UUID
    client_id: str
    kind: str
    name: str
    redirect_uris: list[str]
    enabled: bool
    allow_mcp: bool
    note: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class RejectedAttemptView(BaseModel):
    client_id: str
    redirect_uri: str
    reason: str
    at: datetime


class OAuthClientList(BaseModel):
    items: list[OAuthClientView]
    rejected: list[RejectedAttemptView] = Field(default_factory=list)
    # What the server metadata will say now, so the admin sees the effect of a change.
    advertises_metadata_documents: bool


class OAuthClientCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1, max_length=clients.MAX_CLIENT_ID_LENGTH)
    kind: str = Field(max_length=16)
    name: str = Field(min_length=1, max_length=120)
    redirect_uris: list[str] = Field(min_length=1, max_length=10)
    enabled: bool = True
    allow_mcp: bool = True
    note: Optional[str] = Field(default=None, max_length=1000)


class OAuthClientUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: Optional[str] = Field(default=None, min_length=1, max_length=clients.MAX_CLIENT_ID_LENGTH)
    kind: Optional[str] = Field(default=None, max_length=16)
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    redirect_uris: Optional[list[str]] = Field(default=None, min_length=1, max_length=10)
    enabled: Optional[bool] = None
    allow_mcp: Optional[bool] = None
    note: Optional[str] = Field(default=None, max_length=1000)


def _view(row: OAuthClientConfig) -> OAuthClientView:
    return OAuthClientView(
        id=row.id,
        client_id=row.client_id,
        kind=row.kind,
        name=row.name,
        redirect_uris=list(row.redirect_uris or []),
        enabled=row.enabled,
        allow_mcp=row.allow_mcp,
        note=row.note,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _clash() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail="a tool with that client id is already configured"
    )


async def _get(session: AsyncSession, row_id: uuid.UUID) -> OAuthClientConfig:
    row = await session.get(OAuthClientConfig, row_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such tool")
    return row


@router.get("", response_model=OAuthClientList, summary="Tools allowed to ask for access")
async def list_clients(
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> OAuthClientList:
    rows = await clients.list_configured(session)
    await session.commit()

    def still_refused(attempt: dict) -> bool:
        """Would this attempt be turned away today? One that has since been allowed is history."""

        tool = clients.match_client(rows, attempt["client_id"])
        return tool is None or not tool.redirect_allowed(attempt.get("redirect_uri", ""))

    rejected = [
        RejectedAttemptView(
            client_id=r["client_id"],
            redirect_uri=r.get("redirect_uri", ""),
            reason=r.get("reason", ""),
            at=datetime.fromtimestamp(r["at"], tz=timezone.utc),
        )
        for r in await clients.recent_rejected(infra.redis)
        if still_refused(r)
    ]
    return OAuthClientList(
        items=[_view(r) for r in rows],
        rejected=rejected,
        advertises_metadata_documents=any(
            r.enabled and r.kind == clients.KIND_METADATA_URL for r in rows
        ),
    )


@router.post(
    "", response_model=OAuthClientView, status_code=status.HTTP_201_CREATED, summary="Allow a new tool"
)
async def create_client(
    payload: OAuthClientCreate, session: AsyncSession = Depends(get_db_session)
) -> OAuthClientView:
    try:
        client_id, redirects = clients.validate_client_config(
            payload.client_id, payload.kind, payload.redirect_uris
        )
    except clients.ClientConfigError as exc:
        raise _bad(str(exc)) from exc
    row = OAuthClientConfig(
        client_id=client_id,
        kind=payload.kind,
        name=payload.name.strip(),
        redirect_uris=redirects,
        enabled=payload.enabled,
        allow_mcp=payload.allow_mcp,
        note=(payload.note or "").strip() or None,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise _clash() from exc
    await session.refresh(row)
    logger.warning(
        "platform admin allowed oauth client %r (%s) with redirects %s",
        row.client_id, row.kind, row.redirect_uris,
    )
    return _view(row)


@router.patch("/{row_id}", response_model=OAuthClientView, summary="Edit a tool, or switch it on or off")
async def update_client(
    row_id: uuid.UUID,
    payload: OAuthClientUpdate,
    session: AsyncSession = Depends(get_db_session),
) -> OAuthClientView:
    row = await _get(session, row_id)
    client_id = payload.client_id if payload.client_id is not None else row.client_id
    kind = payload.kind if payload.kind is not None else row.kind
    redirects = payload.redirect_uris if payload.redirect_uris is not None else list(row.redirect_uris or [])
    try:
        client_id, redirects = clients.validate_client_config(client_id, kind, redirects)
    except clients.ClientConfigError as exc:
        raise _bad(str(exc)) from exc
    before = (row.client_id, row.kind, list(row.redirect_uris or []), row.enabled, row.allow_mcp)
    row.client_id, row.kind, row.redirect_uris = client_id, kind, redirects
    if payload.name is not None:
        row.name = payload.name.strip()
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.allow_mcp is not None:
        row.allow_mcp = payload.allow_mcp
    if payload.note is not None:
        row.note = payload.note.strip() or None
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise _clash() from exc
    await session.refresh(row)
    logger.warning(
        "platform admin changed oauth client %r: %s -> %s",
        row.client_id, before, (row.client_id, row.kind, row.redirect_uris, row.enabled, row.allow_mcp),
    )
    return _view(row)


@router.delete(
    "/{row_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response, summary="Remove a tool"
)
async def delete_client(row_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)) -> Response:
    row = await _get(session, row_id)
    name = row.client_id
    await session.delete(row)
    await session.commit()
    logger.warning("platform admin removed oauth client %r", name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


admin_oauth_clients_router = router
