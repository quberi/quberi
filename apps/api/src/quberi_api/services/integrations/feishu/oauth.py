"""Feishu user-level OAuth (v2, standard OAuth2 authorization-code flow).

Endpoints (Feishu 2024 v2 OAuth)::

    authorize:  https://accounts.feishu.cn/open-apis/authen/v1/authorize
    token:      https://open.feishu.cn/open-apis/authen/v2/oauth/token

Flow
----
1. :func:`build_authorize_url` — mint a CSRF ``state`` row, return the
   consent-screen URL. The frontend redirects the browser there.
2. Feishu redirects back to our ``redirect_uri`` with ``?code=&state=``.
3. :func:`exchange_code` — validate ``state``, swap ``code`` for
   ``access_token`` + ``refresh_token``, persist an
   :class:`IntegrationUserToken` (encrypted).
4. :func:`get_valid_user_token` — return a live access token for a
   (integration, user), refreshing via ``refresh_token`` when expired.

The user token is what lets retrieval act *as that user* against
Feishu — the linchpin of permission-faithful cross-system RAG.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.core.config import Settings, get_settings
from quberi_api.domain.integrations.models import (
    Integration,
    IntegrationOAuthState,
    IntegrationUserToken,
)
from quberi_api.services.integrations.feishu.client import FeishuError
from quberi_api.services.security.crypto import seal, unseal

_log = logging.getLogger(__name__)


ACCOUNTS_AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_URL_PATH = "/authen/v2/oauth/token"

# Scopes the user consents to. ``offline_access`` yields a refresh_token
# for persistent connections (auto-renew, no re-consent every ~2h).
# Folder browsing needs ``drive:drive:readonly``. All three must be
# enabled in the Feishu console + published, or OAuth 20027s.
DEFAULT_USER_SCOPES = (
    "docx:document:readonly drive:drive:readonly offline_access"
)

_STATE_TTL = timedelta(minutes=10)
# Refresh a little early so a request that arrives right at expiry
# doesn't 401 mid-flight.
_EXPIRY_SKEW = timedelta(seconds=120)


class OAuthError(RuntimeError):
    """OAuth-flow failure (bad state, token exchange rejected, …)."""


@dataclass(frozen=True)
class TokenExchangeResult:
    access_token: str
    refresh_token: Optional[str]
    expires_in: int
    refresh_expires_in: Optional[int]
    scope: Optional[str]


# ---------------------------------------------------------------------------
# 1. Authorize URL
# ---------------------------------------------------------------------------


async def build_authorize_url(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    redirect_uri: str,
    redirect_after: Optional[str] = None,
    scopes: str = DEFAULT_USER_SCOPES,
) -> str:
    """Mint a state nonce and return the Feishu consent URL."""

    settings = get_settings()
    if not settings.feishu_app_id:
        raise OAuthError("Feishu app_id is not configured")

    state = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    row = IntegrationOAuthState(
        id=uuid.uuid4(),
        state=state,
        organization_id=organization_id,
        user_id=user_id,
        provider="feishu",
        redirect_after=redirect_after,
        created_at=now,
        expires_at=now + _STATE_TTL,
    )
    # ``build_authorize_url`` is called from a request handler that hasn't
    # begun a transaction; open one just for this insert.
    async with session.begin():
        session.add(row)

    params = {
        "client_id": settings.feishu_app_id,
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state,
        "response_type": "code",
    }
    return f"{ACCOUNTS_AUTHORIZE_URL}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# 2 + 3. Callback → token exchange → persist
# ---------------------------------------------------------------------------


async def exchange_code(
    session: AsyncSession,
    *,
    code: str,
    state: str,
    redirect_uri: str,
) -> IntegrationUserToken:
    """Validate ``state``, swap ``code`` for tokens, persist the grant."""

    # -- validate + consume state --
    state_row = (
        await session.execute(
            select(IntegrationOAuthState).where(
                IntegrationOAuthState.state == state
            )
        )
    ).scalar_one_or_none()
    if state_row is None:
        raise OAuthError("unknown or already-used OAuth state")
    now = datetime.now(timezone.utc)
    expires_at = state_row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    org_id = state_row.organization_id
    user_id = state_row.user_id
    # Consume it (single use) regardless of what happens next.
    await session.commit()
    async with session.begin():
        await session.delete(state_row)
    if expires_at < now:
        raise OAuthError("OAuth state expired; please retry the connection")

    # -- exchange code for tokens --
    result = await _token_request(
        grant_type="authorization_code",
        code=code,
        redirect_uri=redirect_uri,
    )

    # -- ensure an Integration row exists for this org --
    integration = await _ensure_integration(session, organization_id=org_id)

    # -- upsert the per-user token --
    token = await _upsert_user_token(
        session,
        integration_id=integration.id,
        user_id=user_id,
        result=result,
    )
    return token


# ---------------------------------------------------------------------------
# 4. Get / refresh a live token
# ---------------------------------------------------------------------------


async def get_valid_user_token(
    session: AsyncSession,
    *,
    integration_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Optional[str]:
    """Return a live access token for (integration, user), or None.

    Refreshes transparently when the stored token is within the skew
    window of expiry. Returns None when the user has no grant (caller
    should prompt them to connect).
    """

    row = (
        await session.execute(
            select(IntegrationUserToken).where(
                IntegrationUserToken.integration_id == integration_id,
                IntegrationUserToken.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    now = datetime.now(timezone.utc)
    expires_at = row.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at is None or expires_at - _EXPIRY_SKEW > now:
        return unseal(row.access_token_encrypted)

    # -- expired: refresh --
    refresh_token = (
        unseal(row.refresh_token_encrypted)
        if row.refresh_token_encrypted
        else None
    )
    if not refresh_token:
        _log.info(
            "feishu token for user %s expired and no refresh_token; "
            "user must re-consent",
            user_id,
        )
        return None
    try:
        result = await _token_request(
            grant_type="refresh_token", refresh_token=refresh_token
        )
    except (OAuthError, FeishuError) as exc:
        _log.warning("feishu token refresh failed for user %s: %s", user_id, exc)
        return None

    await _apply_token_result(session, row=row, result=result)
    return result.access_token


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _token_request(
    *,
    grant_type: str,
    code: Optional[str] = None,
    refresh_token: Optional[str] = None,
    redirect_uri: Optional[str] = None,
) -> TokenExchangeResult:
    settings: Settings = get_settings()
    if not settings.feishu_app_id or settings.feishu_app_secret is None:
        raise OAuthError("Feishu credentials are not configured")

    body: dict = {
        "grant_type": grant_type,
        "client_id": settings.feishu_app_id,
        "client_secret": settings.feishu_app_secret.get_secret_value(),
    }
    if grant_type == "authorization_code":
        body["code"] = code
        if redirect_uri:
            body["redirect_uri"] = redirect_uri
    elif grant_type == "refresh_token":
        body["refresh_token"] = refresh_token

    url = f"{settings.feishu_base_url}{TOKEN_URL_PATH}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        resp = await http.post(url, json=body)
    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise OAuthError(f"token endpoint returned non-JSON: {resp.text[:200]}") from exc

    # v2 OAuth returns code=0 on success in the envelope-style body, but
    # some deployments return a bare OAuth2 body. Handle both.
    envelope_code = payload.get("code")
    if envelope_code not in (0, None):
        raise OAuthError(
            f"feishu token exchange failed [{envelope_code}]: "
            f"{payload.get('error_description') or payload.get('msg') or payload}"
        )
    access_token = payload.get("access_token")
    if not access_token:
        raise OAuthError(f"token endpoint returned no access_token: {payload}")

    return TokenExchangeResult(
        access_token=access_token,
        refresh_token=payload.get("refresh_token"),
        expires_in=int(payload.get("expires_in", 7200) or 7200),
        refresh_expires_in=(
            int(payload["refresh_token_expires_in"])
            if payload.get("refresh_token_expires_in") is not None
            else None
        ),
        scope=payload.get("scope"),
    )


async def _ensure_integration(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> Integration:
    row = (
        await session.execute(
            select(Integration).where(
                Integration.organization_id == organization_id,
                Integration.provider == "feishu",
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    await session.commit()
    async with session.begin():
        row = Integration(
            id=uuid.uuid4(),
            organization_id=organization_id,
            provider="feishu",
            display_name="Feishu",
            status="active",
            app_credentials_encrypted=None,  # platform default app
            config={},
        )
        session.add(row)
        await session.flush()
        await session.refresh(row, attribute_names=["created_at", "updated_at"])
    return row


async def _upsert_user_token(
    session: AsyncSession,
    *,
    integration_id: uuid.UUID,
    user_id: uuid.UUID,
    result: TokenExchangeResult,
) -> IntegrationUserToken:
    existing = (
        await session.execute(
            select(IntegrationUserToken).where(
                IntegrationUserToken.integration_id == integration_id,
                IntegrationUserToken.user_id == user_id,
            )
        )
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=result.expires_in)
    refresh_expires_at = (
        now + timedelta(seconds=result.refresh_expires_in)
        if result.refresh_expires_in
        else None
    )

    await session.commit()
    async with session.begin():
        if existing is None:
            existing = IntegrationUserToken(
                id=uuid.uuid4(),
                integration_id=integration_id,
                user_id=user_id,
                access_token_encrypted=seal(result.access_token),
                refresh_token_encrypted=(
                    seal(result.refresh_token) if result.refresh_token else None
                ),
                scopes=result.scope,
                expires_at=expires_at,
                refresh_expires_at=refresh_expires_at,
            )
            session.add(existing)
        else:
            existing.access_token_encrypted = seal(result.access_token)
            if result.refresh_token:
                existing.refresh_token_encrypted = seal(result.refresh_token)
            existing.scopes = result.scope
            existing.expires_at = expires_at
            existing.refresh_expires_at = refresh_expires_at
        await session.flush()
        await session.refresh(existing, attribute_names=["updated_at"])
    return existing


async def _apply_token_result(
    session: AsyncSession,
    *,
    row: IntegrationUserToken,
    result: TokenExchangeResult,
) -> None:
    now = datetime.now(timezone.utc)
    await session.commit()
    async with session.begin():
        row.access_token_encrypted = seal(result.access_token)
        if result.refresh_token:
            row.refresh_token_encrypted = seal(result.refresh_token)
        row.expires_at = now + timedelta(seconds=result.expires_in)
        if result.refresh_expires_in:
            row.refresh_expires_at = now + timedelta(
                seconds=result.refresh_expires_in
            )
        row.scopes = result.scope or row.scopes
