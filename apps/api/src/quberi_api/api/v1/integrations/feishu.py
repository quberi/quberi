"""Feishu integration REST surface.

Early routes are read-only smoke tests. As the org-scoped ``integrations``
table lands we'll add CRUD for BYO credentials + a proper OAuth callback
for user-side authorization.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.admin_gate import require_org_admin
from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.domain.documents.models import Document
from quberi_api.services import feishu_sources
from quberi_api.services.integrations.feishu import (
    FeishuAuthError,
    FeishuError,
    FeishuNotConfiguredError,
    get_platform_client,
)
from quberi_api.services.integrations.feishu.docs import (
    get_document_text,
    list_files,
)

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations/feishu", tags=["integrations-feishu"])


class PingResponse(BaseModel):
    ok: bool
    has_token: bool
    token_prefix: str | None = None
    detail: str | None = None


@router.get(
    "/ping",
    response_model=PingResponse,
    summary="Smoke test: fetch a Feishu tenant_access_token",
)
async def feishu_ping(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> PingResponse:
    """Prove the platform Feishu credentials + Redis token cache work.

    A successful response means ``QUBERI_FEISHU_APP_ID`` and
    ``QUBERI_FEISHU_APP_SECRET`` are valid and the app has been
    published in the Feishu console.

    Organisation owners and admins only: the call uses the platform's shared
    app credentials.
    """
    await require_org_admin(caller, session)

    try:
        client = await get_platform_client()
        result = await client.ping()
        return PingResponse(
            ok=True,
            has_token=result.get("has_token", False),
            token_prefix=result.get("token_prefix"),
        )
    except FeishuNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail=str(exc),
        )
    except FeishuAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        )
    except FeishuError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        )


# ---------------------------------------------------------------------------
# Docs — list & fetch
# ---------------------------------------------------------------------------


class FeishuFileView(BaseModel):
    token: str
    name: str
    type: str
    parent_token: str | None = None
    url: str
    owner_id: str | None = None
    created_time: str | None = None
    modified_time: str | None = None


class FeishuFileList(BaseModel):
    items: list[FeishuFileView]
    total: int


@router.get(
    "/files",
    response_model=FeishuFileList,
    summary="List cloud-drive files (root or specified folder)",
)
async def feishu_list_files(
    folder_token: str | None = None,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> FeishuFileList:
    """List a folder through the shared app.

    Organisation owners and admins only, and only folders this organisation
    has connected: every organisation shares one Feishu app, so without this
    an admin of one organisation could read another's folders.
    """
    await require_org_admin(caller, session)
    if not folder_token or not await feishu_sources.org_has_claim(
        session, organization_id=caller.organization.id, folder_token=folder_token
    ):
        await session.commit()
        # Same answer for "not connected" and "belongs to someone else".
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="folder is not connected to this organisation",
        )
    await session.commit()
    try:
        client = await get_platform_client()
        rows = await list_files(client, folder_token=folder_token)
    except FeishuNotConfiguredError as exc:
        raise HTTPException(status_code=status.HTTP_412_PRECONDITION_FAILED, detail=str(exc))
    except FeishuError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return FeishuFileList(
        items=[
            FeishuFileView(
                token=r.token,
                name=r.name,
                type=r.type,
                parent_token=r.parent_token,
                url=r.url,
                owner_id=r.owner_id,
                created_time=r.created_time,
                modified_time=r.modified_time,
            )
            for r in rows
        ],
        total=len(rows),
    )


class FeishuDocContent(BaseModel):
    document_id: str
    content: str
    length: int


@router.get(
    "/documents/{document_id}/content",
    response_model=FeishuDocContent,
    summary="Fetch the plain-text rendering of a Feishu docx",
)
async def feishu_document_content(
    document_id: str,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> FeishuDocContent:
    """Plain text of a Feishu docx this organisation has already synced.

    Organisation owners and admins only, and only documents that exist in this
    organisation (the shared app can read other organisations' documents too).
    """
    await require_org_admin(caller, session)
    synced = (
        await session.execute(
            select(Document.id)
            .where(
                Document.organization_id == caller.organization.id,
                Document.external_ref["doc_token"].astext == document_id,
            )
            .limit(1)
        )
    ).first()
    await session.commit()
    if synced is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="document is not synced into this organisation",
        )
    try:
        client = await get_platform_client()
        content = await get_document_text(client, document_id=document_id)
    except FeishuNotConfiguredError as exc:
        raise HTTPException(status_code=status.HTTP_412_PRECONDITION_FAILED, detail=str(exc))
    except FeishuError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return FeishuDocContent(document_id=document_id, content=content, length=len(content))


# ---------------------------------------------------------------------------
# OAuth (user-level authorization)
# ---------------------------------------------------------------------------


class AuthorizeUrlResponse(BaseModel):
    authorize_url: str


def _redirect_uri() -> str:
    """Compute the OAuth callback URL from the configured public base."""
    from quberi_api.core.config import get_settings

    base = get_settings().public_base_url
    if not base:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="QUBERI_PUBLIC_BASE_URL is not configured; OAuth needs a "
            "public callback URL (e.g. a Cloudflare Tunnel hostname).",
        )
    return f"{base.rstrip('/')}/api/v1/integrations/feishu/oauth/callback"


@router.post(
    "/oauth/authorize-url",
    response_model=AuthorizeUrlResponse,
    summary="Mint a Feishu consent-screen URL for the current user",
)
async def feishu_authorize_url(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AuthorizeUrlResponse:
    from quberi_api.services.integrations.feishu.oauth import (
        OAuthError,
        build_authorize_url,
    )

    try:
        url = await build_authorize_url(
            session,
            organization_id=caller.organization.id,
            user_id=caller.user.id,
            redirect_uri=_redirect_uri(),
        )
    except OAuthError as exc:
        raise HTTPException(status_code=status.HTTP_412_PRECONDITION_FAILED, detail=str(exc))
    return AuthorizeUrlResponse(authorize_url=url)


@router.get(
    "/oauth/callback",
    summary="Feishu OAuth redirect target — exchanges code for tokens",
)
async def feishu_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """Browser lands here after Feishu consent.

    We're not behind ``require_caller`` — the user's Quberi session
    isn't carried through Feishu's redirect. The ``state`` nonce (minted
    in ``authorize-url`` while authenticated) is what ties the callback
    back to the right (org, user).
    """

    from fastapi.responses import HTMLResponse

    from quberi_api.services.integrations.feishu.oauth import (
        OAuthError,
        exchange_code,
    )

    if error:
        return HTMLResponse(_callback_html(ok=False, message=f"Feishu error: {error}"))
    if not code or not state:
        return HTMLResponse(
            _callback_html(ok=False, message="missing code/state"), status_code=400
        )
    try:
        await exchange_code(
            session, code=code, state=state, redirect_uri=_redirect_uri()
        )
    except OAuthError as exc:
        return HTMLResponse(_callback_html(ok=False, message=str(exc)), status_code=400)
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(
            _callback_html(ok=False, message=f"unexpected: {exc}"), status_code=500
        )
    return HTMLResponse(_callback_html(ok=True, message="Feishu connected."))


def _callback_html(*, ok: bool, message: str) -> str:
    """Tiny self-closing page — signals the opener window then closes."""
    status_word = "success" if ok else "error"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Feishu OAuth</title></head><body style="font-family:system-ui;padding:40px;text-align:center">
<h2>{'✅' if ok else '⚠️'} {message}</h2>
<p>You can close this window.</p>
<script>
  try {{ window.opener && window.opener.postMessage({{source:'quberi-feishu-oauth',status:'{status_word}'}}, '*'); }} catch (e) {{}}
  setTimeout(function(){{ window.close(); }}, 1200);
</script>
</body></html>"""


class ConnectionStatus(BaseModel):
    connected: bool
    external_user_id: str | None = None
    scopes: str | None = None
    expires_at: str | None = None


@router.get(
    "/oauth/status",
    response_model=ConnectionStatus,
    summary="Whether the current user has a live Feishu grant",
)
async def feishu_oauth_status(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ConnectionStatus:
    from sqlalchemy import select

    from quberi_api.domain.integrations.models import (
        Integration,
        IntegrationUserToken,
    )

    integration = (
        await session.execute(
            select(Integration).where(
                Integration.organization_id == caller.organization.id,
                Integration.provider == "feishu",
            )
        )
    ).scalar_one_or_none()
    if integration is None:
        return ConnectionStatus(connected=False)
    row = (
        await session.execute(
            select(IntegrationUserToken).where(
                IntegrationUserToken.integration_id == integration.id,
                IntegrationUserToken.user_id == caller.user.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return ConnectionStatus(connected=False)
    return ConnectionStatus(
        connected=True,
        external_user_id=row.external_user_id,
        scopes=row.scopes,
        expires_at=row.expires_at.isoformat() if row.expires_at else None,
    )


# ---------------------------------------------------------------------------
# Folder tree browse (user token) — powers the frontend picker
# ---------------------------------------------------------------------------


class FolderNodeView(BaseModel):
    token: str
    name: str
    type: str
    has_children: bool


class FolderBrowseResponse(BaseModel):
    items: list[FolderNodeView]
    needs_auth: bool = False


@router.get(
    "/folders",
    response_model=FolderBrowseResponse,
    summary="Browse one level of the user's Feishu folder tree",
)
async def feishu_browse_folders(
    folder_token: str | None = None,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> FolderBrowseResponse:
    from sqlalchemy import select

    from quberi_api.core.config import get_settings
    from quberi_api.domain.integrations.models import Integration
    from quberi_api.services.integrations.feishu.docs import browse_folder_as_user
    from quberi_api.services.integrations.feishu.oauth import get_valid_user_token

    integration = (
        await session.execute(
            select(Integration).where(
                Integration.organization_id == caller.organization.id,
                Integration.provider == "feishu",
            )
        )
    ).scalar_one_or_none()
    if integration is None:
        return FolderBrowseResponse(items=[], needs_auth=True)

    token = await get_valid_user_token(
        session, integration_id=integration.id, user_id=caller.user.id
    )
    if not token:
        return FolderBrowseResponse(items=[], needs_auth=True)

    try:
        nodes = await browse_folder_as_user(
            base_url=get_settings().feishu_base_url,
            user_access_token=token,
            folder_token=folder_token,
        )
    except FeishuError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return FolderBrowseResponse(
        items=[
            FolderNodeView(
                token=n.token, name=n.name, type=n.type, has_children=n.has_children
            )
            for n in nodes
        ]
    )


# ---------------------------------------------------------------------------
# Webhook receiver (event subscription)
# ---------------------------------------------------------------------------


@router.post(
    "/webhook",
    summary="Feishu event subscription callback (url_verification + file events)",
)
async def feishu_webhook(
    body: dict,
    session: AsyncSession = Depends(get_db_session),
):
    from sqlalchemy import select

    from quberi_api.domain.documents.models import DocumentSource
    from quberi_api.services.integrations.feishu.events import (
        EventVerificationError,
        decode_event,
        is_file_event,
    )
    from quberi_api.workers.enqueue import enqueue_feishu_sync

    try:
        decoded = decode_event(body)
    except EventVerificationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))

    # 1. URL verification handshake — echo the challenge.
    if decoded.is_url_verification:
        return {"challenge": decoded.challenge}

    # 2. File events → enqueue re-sync of matching feishu sources.
    if is_file_event(decoded.event_type):
        # MVP: re-sync every active feishu source. Idempotent + cheap
        # (checksum-skip). Precise folder→source matching lands later.
        rows = (
            await session.execute(
                select(DocumentSource).where(
                    DocumentSource.kind == "feishu",
                    DocumentSource.status == "active",
                )
            )
        ).scalars().all()
        await session.commit()
        for row in rows:
            await enqueue_feishu_sync(row.id)
        _log.info(
            "feishu webhook %s → enqueued %d source re-syncs",
            decoded.event_type, len(rows),
        )

    return {"code": 0}


feishu_router = router
