"""OAuth for outside tools (first: DeepSeek Harness).

Two groups of routes:

* **Consent** (``/oauth/consent``, ``/oauth/authorize``,
  ``/oauth/enable-external-tools``) - called by Quberi's own web page with the
  person's normal session. Guests are refused.
* **Token** (``/oauth/token``, ``/oauth/revoke``) - called by the tool itself.
  No session; the one-time code + PKCE verifier (or refresh token) is the
  credential. Rate limited per client address.

The consent page lives in the web app, so the authorisation endpoint in the
discovery document points there. The API never redirects the browser itself:
it validates the request, creates the code and hands the final address back to
the page, so an invalid ``redirect_uri`` can never be bounced to.
"""

from __future__ import annotations

import uuid
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session, get_infrastructure
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.api.v1.guest_chat import client_ip
from quberi_api.core.config import get_settings
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.identity.models import Organization
from quberi_api.infrastructure.registry import Infrastructure
from quberi_api.services import org_membership as org_membership_service
from quberi_api.services.authz import caller_has_org_role
from quberi_api.services.oauth import clients, core

router = APIRouter(prefix="/oauth", tags=["oauth"])

_TOKEN_RATE_LIMIT = 30  # per client address per minute
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


# ---------------------------------------------------------------------------
# Consent (called by the web page, with the person's session)
# ---------------------------------------------------------------------------


class ConsentOrganization(BaseModel):
    organization_id: uuid.UUID
    name: str
    tier: str
    is_current: bool
    external_tools_enabled: bool
    # An owner/admin may switch it on from the consent page itself.
    can_enable: bool


class ConsentView(BaseModel):
    client_id: str
    client_name: str
    scope: str
    redirect_uri: str
    organizations: list[ConsentOrganization]


class AuthorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1, max_length=64)
    redirect_uri: str = Field(min_length=1, max_length=200)
    scope: Optional[str] = Field(default=None, max_length=200)
    state: str = Field(default="", max_length=512)
    code_challenge: str = Field(min_length=1, max_length=128)
    code_challenge_method: str = Field(default="S256", max_length=16)
    organization_id: uuid.UUID


class AuthorizeResponse(BaseModel):
    redirect_to: str


class EnableExternalToolsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: uuid.UUID


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _require_real_user(caller: CallerContext) -> None:
    if caller.is_guest:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="sign in to connect a tool",
        )


def _validate_client(
    client_id: str, redirect_uri: str, scope: Optional[str]
) -> tuple[clients.OAuthClient, str]:
    client = clients.get_client(client_id)
    if client is None:
        raise _bad_request("unknown application")
    if not clients.is_valid_redirect_uri(redirect_uri):
        raise _bad_request("the redirect address is not allowed")
    normalised = clients.normalise_scope(scope)
    if normalised is None:
        raise _bad_request("unsupported scope")
    return client, normalised


@router.get("/consent", response_model=ConsentView, summary="What the consent page shows")
async def consent(
    client_id: str,
    redirect_uri: str,
    scope: Optional[str] = None,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ConsentView:
    _require_real_user(caller)
    client, normalised = _validate_client(client_id, redirect_uri, scope)

    mine = [
        o
        for o in await org_membership_service.list_my_organizations(
            session, identity_id=caller.user.identity_id
        )
        if not o.is_pending
    ]
    flags = {
        row.id: row.allow_external_tools
        for row in (
            await session.execute(
                select(Organization).where(
                    Organization.id.in_([o.organization_id for o in mine])
                )
            )
        ).scalars()
    }
    items: list[ConsentOrganization] = []
    for o in mine:
        enabled = bool(flags.get(o.organization_id, False))
        can_enable = False
        if not enabled:
            can_enable = await caller_has_org_role(
                session,
                organization_id=o.organization_id,
                user_id=o.user_id,
                allowed_codes=("owner", "admin"),
            )
        items.append(
            ConsentOrganization(
                organization_id=o.organization_id,
                name=o.name,
                tier=o.tier,
                is_current=o.organization_id == caller.organization.id,
                external_tools_enabled=enabled,
                can_enable=can_enable,
            )
        )
    await session.commit()
    return ConsentView(
        client_id=client.client_id,
        client_name=client.name,
        scope=normalised,
        redirect_uri=redirect_uri,
        organizations=items,
    )


@router.post("/authorize", response_model=AuthorizeResponse, summary="Approve a tool")
async def authorize(
    payload: AuthorizeRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> AuthorizeResponse:
    _require_real_user(caller)
    client, scope = _validate_client(
        payload.client_id, payload.redirect_uri, payload.scope
    )
    if payload.code_challenge_method != "S256" or not core.is_valid_challenge(
        payload.code_challenge
    ):
        raise _bad_request("PKCE with S256 is required")

    membership = next(
        (
            o
            for o in await org_membership_service.list_my_organizations(
                session, identity_id=caller.user.identity_id
            )
            if o.organization_id == payload.organization_id and not o.is_pending
        ),
        None,
    )
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="you are not a member of that organization",
        )
    org = await session.get(Organization, payload.organization_id)
    await session.commit()
    if org is None or org.status != "active" or not org.allow_external_tools:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this organization does not allow outside tools",
        )

    code = await core.create_authorization_code(
        infra.redis,
        client_id=client.client_id,
        redirect_uri=payload.redirect_uri,
        code_challenge=payload.code_challenge,
        user_id=membership.user_id,
        organization_id=payload.organization_id,
        scope=scope,
    )
    query = {"code": code}
    if payload.state:
        query["state"] = payload.state
    return AuthorizeResponse(redirect_to=f"{payload.redirect_uri}?{urlencode(query)}")


@router.post(
    "/enable-external-tools",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Owner/admin switches on outside tools for their organization",
)
async def enable_external_tools(
    payload: EnableExternalToolsRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    _require_real_user(caller)
    membership = next(
        (
            o
            for o in await org_membership_service.list_my_organizations(
                session, identity_id=caller.user.identity_id
            )
            if o.organization_id == payload.organization_id and not o.is_pending
        ),
        None,
    )
    allowed = membership is not None and await caller_has_org_role(
        session,
        organization_id=payload.organization_id,
        user_id=membership.user_id,
        allowed_codes=("owner", "admin"),
    )
    await session.commit()
    if not allowed or membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only organisation owners or admins can change this",
        )
    async with session.begin():
        org = await session.get(Organization, payload.organization_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        if not org.allow_external_tools:
            org.allow_external_tools = True
            session.add(
                AuditEvent(
                    organization_id=org.id,
                    actor_type="user",
                    actor_id=membership.user_id,
                    action="organization.external_tools_changed",
                    resource_type="organization",
                    resource_id=org.id,
                    payload={"enabled": True},
                )
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Token endpoint (called by the tool)
# ---------------------------------------------------------------------------


def _error(exc: core.OAuthError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": exc.error, "error_description": exc.description},
        headers=_NO_STORE,
    )


@router.post("/token", summary="Exchange a code or refresh token")
async def token(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> JSONResponse:
    try:
        await core.rate_limit(
            infra.redis,
            key=f"quberi:oauth:token:{client_ip(request)}",
            limit=_TOKEN_RATE_LIMIT,
            window_seconds=60,
        )
        form = await request.form()
        grant_type = str(form.get("grant_type", ""))
        client_id = str(form.get("client_id", ""))
        client = clients.get_client(client_id)
        if client is None:
            raise core.OAuthError("invalid_client", "unknown application", status=401)

        if grant_type == "authorization_code":
            issued = await core.exchange_code(
                session,
                infra.redis,
                code=str(form.get("code", "")),
                code_verifier=str(form.get("code_verifier", "")),
                redirect_uri=str(form.get("redirect_uri", "")),
                client_id=client.client_id,
                client_name=client.name,
            )
        elif grant_type == "refresh_token":
            issued = await core.refresh_tokens(
                session,
                refresh_token=str(form.get("refresh_token", "")),
                client_id=client.client_id,
            )
        else:
            raise core.OAuthError("unsupported_grant_type", "unsupported grant_type")
    except core.OAuthError as exc:
        return _error(exc)
    return JSONResponse(content=issued.as_response(), headers=_NO_STORE)


@router.post("/revoke", summary="Revoke a token (RFC 7009)")
async def revoke(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> JSONResponse:
    try:
        await core.rate_limit(
            infra.redis,
            key=f"quberi:oauth:token:{client_ip(request)}",
            limit=_TOKEN_RATE_LIMIT,
            window_seconds=60,
        )
    except core.OAuthError as exc:
        return _error(exc)
    form = await request.form()
    token_value = str(form.get("token", ""))
    if token_value:
        await core.revoke_by_token(session, token_value)
    # Always 200: the caller learns nothing about whether the token existed.
    return JSONResponse(content={}, headers=_NO_STORE)


oauth_router = router


# ---------------------------------------------------------------------------
# Discovery (RFC 8414) - mounted at the application root, see ``main.py``
# ---------------------------------------------------------------------------

well_known_router = APIRouter(tags=["oauth"])


@well_known_router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata() -> JSONResponse:
    settings = get_settings()
    api = (settings.public_base_url or "").rstrip("/")
    web = settings.web_origin or ""
    return JSONResponse(
        content={
            "issuer": api,
            "authorization_endpoint": f"{web}/oauth/authorize",
            "token_endpoint": f"{api}/api/v1/oauth/token",
            "revocation_endpoint": f"{api}/api/v1/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": list(clients.SUPPORTED_SCOPES),
        },
        headers={"Cache-Control": "public, max-age=3600"},
    )
