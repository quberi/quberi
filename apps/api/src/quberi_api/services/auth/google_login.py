"""Google Sign-In (OpenID Connect authorization-code flow).

Unlike Feishu, Google's ``userinfo`` endpoint reliably returns a
platform-verified email (``email_verified: true``) for essentially every
consumer account. That means Google consent alone is enough to resolve or
create an account — there's no separate "bind a verified email" step like
``feishu_login.py`` needs. Email is still the canonical join key so the
same human never ends up with two accounts:

* email already maps to a real account → link Google onto it, sign in;
* email is new **and** the subject already has a placeholder-only
  account → adopt that account by giving it this real email (no fork);
* brand-new → bootstrap a personal account and link Google to it.

Login and "link Google to my already-signed-in account" share the same
callback; the sealed ``state`` carries the purpose (mirrors
``feishu_login.py``'s state handling almost verbatim).
"""

from __future__ import annotations

import logging
import re
import secrets
import time
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.core.config import get_settings
from quberi_api.domain.identity.models import (
    ExternalIdentity,
    Identity,
    Organization,
    User,
)
from quberi_api.services.auth.account_status import (
    best_login_user,
    raise_if_identity_suspended,
)
from quberi_api.services.auth.verification import ResolvedLogin, _find_active_user
from quberi_api.services.onboarding import bootstrap_organization
from quberi_api.services.security import crypto

_log = logging.getLogger(__name__)

_PROVIDER = "google"
_PURPOSE_LOGIN = "google_login"
_PURPOSE_LINK = "google_link"
_STATE_TTL_SECONDS = 600

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
# openid+email+profile is the minimal scope set that yields a verified
# email + display name + avatar; no Drive/Calendar/etc access requested.
LOGIN_SCOPES = "openid email profile"


class GoogleLoginError(Exception):
    """Google login/link could not be completed."""


@dataclass(frozen=True)
class TokenExchangeResult:
    access_token: str
    id_token: Optional[str]
    expires_in: int


# ---------------------------------------------------------------------------
# State (Fernet-sealed nonce; no storage). Mirrors feishu_login._seal_state.
# ---------------------------------------------------------------------------


FLOW_POPUP = "popup"
FLOW_REDIRECT = "redirect"


def _seal_state(
    purpose: str, *, identity_id: Optional[str] = None, flow: str = FLOW_POPUP
) -> str:
    payload = {
        "p": purpose,
        "n": secrets.token_urlsafe(16),
        "exp": int(time.time()) + _STATE_TTL_SECONDS,
        # How the browser reached Google: in a popup (result is postMessage'd
        # back) or by a full-page redirect (result is redirected back to the
        # app). Sealed with the state so the callback can't be told otherwise.
        "f": flow,
    }
    if identity_id is not None:
        payload["iid"] = identity_id
    sealed = crypto.seal(payload)
    assert sealed is not None  # noqa: S101 — payload is never empty
    return sealed


def _read_state(state: str, *, expected_purpose: str) -> dict:
    try:
        payload = crypto.unseal(state)
    except Exception as exc:  # noqa: BLE001 — any decode failure = bad state
        raise GoogleLoginError("invalid state") from exc
    if not isinstance(payload, dict) or payload.get("p") != expected_purpose:
        raise GoogleLoginError("invalid state")
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or int(exp) < int(time.time()):
        raise GoogleLoginError("state expired; please retry")
    return payload


def state_purpose(state: str) -> str:
    """Return the purpose baked into a state, or raise if it's not ours."""

    try:
        payload = crypto.unseal(state)
    except Exception as exc:  # noqa: BLE001
        raise GoogleLoginError("invalid state") from exc
    purpose = payload.get("p") if isinstance(payload, dict) else None
    if purpose not in (_PURPOSE_LOGIN, _PURPOSE_LINK):
        raise GoogleLoginError("invalid state")
    return purpose


def state_flow(state: Optional[str]) -> str:
    """The flow baked into a state; ``popup`` for anything missing/unreadable.

    Never raises: it is used to decide *how to show an error*, including when
    the state itself is the problem, so it must always have an answer.
    Only login states may use the redirect flow — linking runs inside the
    signed-in page and always stays a popup.
    """

    if not state:
        return FLOW_POPUP
    try:
        payload = crypto.unseal(state)
    except Exception:  # noqa: BLE001
        return FLOW_POPUP
    if (
        isinstance(payload, dict)
        and payload.get("p") == _PURPOSE_LOGIN
        and payload.get("f") == FLOW_REDIRECT
    ):
        return FLOW_REDIRECT
    return FLOW_POPUP


# ---------------------------------------------------------------------------
# 1. Authorize URL
# ---------------------------------------------------------------------------


def _authorize_url(*, redirect_uri: str, state: str) -> str:
    settings = get_settings()
    if not settings.google_client_id:
        raise GoogleLoginError("Google client_id is not configured")
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "scope": LOGIN_SCOPES,
        "state": state,
        "response_type": "code",
        # Consistent, verified profile every time; avoids surprise consent
        # variations across repeat logins.
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def build_login_authorize_url(*, redirect_uri: str, flow: str = FLOW_POPUP) -> str:
    if flow not in (FLOW_POPUP, FLOW_REDIRECT):
        raise GoogleLoginError(f"unknown login flow: {flow!r}")
    return _authorize_url(
        redirect_uri=redirect_uri, state=_seal_state(_PURPOSE_LOGIN, flow=flow)
    )


def build_link_authorize_url(*, redirect_uri: str, identity_id: str) -> str:
    """Consent URL for *linking* Google to an already-signed-in account."""

    return _authorize_url(
        redirect_uri=redirect_uri,
        state=_seal_state(_PURPOSE_LINK, identity_id=identity_id),
    )


# ---------------------------------------------------------------------------
# 2. Token exchange + userinfo
# ---------------------------------------------------------------------------


async def _token_request(*, code: str, redirect_uri: str) -> TokenExchangeResult:
    settings = get_settings()
    if not settings.google_client_id or settings.google_client_secret is None:
        raise GoogleLoginError("Google credentials are not configured")

    body = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret.get_secret_value(),
        "redirect_uri": redirect_uri,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        resp = await http.post(TOKEN_URL, data=body)
    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise GoogleLoginError(
            f"token endpoint returned non-JSON: {resp.text[:200]}"
        ) from exc
    if resp.status_code >= 400 or "access_token" not in payload:
        raise GoogleLoginError(
            f"google token exchange failed [{payload.get('error')}]: "
            f"{payload.get('error_description') or payload}"
        )
    return TokenExchangeResult(
        access_token=payload["access_token"],
        id_token=payload.get("id_token"),
        expires_in=int(payload.get("expires_in", 3600) or 3600),
    )


async def _fetch_user_info(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        resp = await http.get(
            USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise GoogleLoginError(
            f"userinfo returned non-JSON: {resp.text[:200]}"
        ) from exc
    if resp.status_code >= 400:
        raise GoogleLoginError(f"userinfo failed: {data}")
    if not data.get("sub"):
        raise GoogleLoginError("Google profile has no stable id (sub)")
    return data


# ---------------------------------------------------------------------------
# 3. Callback → identity → ResolvedLogin (no bind-ticket needed)
# ---------------------------------------------------------------------------


def _slug_for(display_name: str) -> str:
    ascii_name = (
        unicodedata.normalize("NFKD", display_name)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    base = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:40] or "ws"
    return f"{base}-{secrets.token_hex(3)}"


async def _login_existing_identity(
    session: AsyncSession, *, identity_id: uuid.UUID, fallback_name: str
) -> Optional[ResolvedLogin]:
    row = await best_login_user(session, identity_id=identity_id)
    if row is None:
        # A suspended member must not be given a fresh account in its place.
        await raise_if_identity_suspended(session, identity_id=identity_id)
    await session.commit()
    if row is None:
        return None
    user, org = row
    return ResolvedLogin(
        identity_id=str(identity_id),
        user_id=str(user.id),
        organization_id=str(org.id),
        organization_slug=org.slug,
        email=user.email,
        display_name=user.display_name or fallback_name,
        organization_tier=org.tier,
        organization_name=org.name,
    )


def _is_placeholder_email(email: Optional[str]) -> bool:
    return bool(email) and email.strip().lower().endswith("@users.quberi.local")


async def _provisional_account_for_subject(
    session: AsyncSession, subject: str
) -> Optional[dict]:
    ext = (
        await session.execute(
            select(ExternalIdentity).where(
                ExternalIdentity.provider == _PROVIDER,
                ExternalIdentity.subject == subject,
            )
        )
    ).scalar_one_or_none()
    if ext is None:
        return None
    row = (
        await session.execute(
            select(User, Organization)
            .join(Organization, Organization.id == User.organization_id)
            .where(User.identity_id == ext.identity_id)
            .where(User.status != "archived")
            .order_by(Organization.tier.desc(), User.created_at.asc())
            .limit(1)
        )
    ).first()
    if row is None:
        return {
            "identity_id": ext.identity_id,
            "user_id": None,
            "is_placeholder": True,
            "orphan": True,
        }
    user, _org = row
    return {
        "identity_id": ext.identity_id,
        "user_id": str(user.id),
        "is_placeholder": _is_placeholder_email(user.email),
        "orphan": False,
    }


async def _attach_subject_to(
    session: AsyncSession,
    *,
    subject: str,
    identity_id: uuid.UUID,
    name: str,
    provisional: Optional[dict],
) -> None:
    """Point the Google ``subject`` at ``identity_id`` (call inside a txn).

    Same rules as ``feishu_login._attach_subject_to``: insert if unlinked,
    no-op if already linked here, repoint+archive if the old link was an
    orphan/placeholder, refuse if it's a real, different account.
    """

    existing = (
        await session.execute(
            select(ExternalIdentity).where(
                ExternalIdentity.provider == _PROVIDER,
                ExternalIdentity.subject == subject,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        session.add(
            ExternalIdentity(
                identity_id=identity_id,
                provider=_PROVIDER,
                subject=subject,
                attributes={"source": "google_login", "name": name},
            )
        )
        return

    if existing.identity_id == identity_id:
        return  # already linked here — idempotent

    abandonable = provisional is not None and (
        provisional.get("orphan") or provisional.get("is_placeholder")
    )
    if not abandonable:
        raise GoogleLoginError(
            "this Google account is already linked to another Quberi account"
        )

    existing.identity_id = identity_id
    prov_user_id = provisional.get("user_id") if provisional else None
    if prov_user_id:
        stale = await session.get(User, uuid.UUID(prov_user_id))
        # A suspended account is waiting to be reactivated; never retire it here.
        if stale is not None and stale.status != "suspended":
            stale.status = "archived"


async def complete_login(
    session: AsyncSession,
    *,
    code: str,
    state: str,
    redirect_uri: str,
) -> ResolvedLogin:
    """Validate state, resolve or create the account, return a session-ready login.

    Google's userinfo is platform-verified, so — unlike Feishu — this
    always resolves to a usable account in one round trip; there's no
    bind-ticket / pending step.
    """

    _read_state(state, expected_purpose=_PURPOSE_LOGIN)

    try:
        result = await _token_request(code=code, redirect_uri=redirect_uri)
    except GoogleLoginError:
        raise

    info = await _fetch_user_info(result.access_token)
    subject = str(info["sub"])
    display_name = (info.get("name") or info.get("email") or "Google user").strip()
    email = (info.get("email") or "").strip().lower()
    email_verified = bool(info.get("email_verified"))

    if not email or not email_verified:
        # Extremely rare for a consumer Google account, but don't silently
        # create an account keyed on an address we can't vouch for.
        raise GoogleLoginError(
            "Google account has no verified email; cannot sign in"
        )

    # Already linked to a working account? → sign straight in.
    ext = (
        await session.execute(
            select(ExternalIdentity).where(
                ExternalIdentity.provider == _PROVIDER,
                ExternalIdentity.subject == subject,
            )
        )
    ).scalar_one_or_none()
    if ext is not None:
        resolved = await _login_existing_identity(
            session, identity_id=ext.identity_id, fallback_name=display_name
        )
        if resolved is not None:
            return resolved
        # Orphan link (user archived) — fall through and re-provision below.

    provisional = await _provisional_account_for_subject(session, subject)
    found = await _find_active_user(session, email)
    found_data = None
    if found is not None:
        user, org = found
        found_data = {
            "identity_id": user.identity_id,
            "user_id": str(user.id),
            "org_id": str(org.id),
            "org_slug": org.slug,
            "org_name": org.name,
            "email": user.email,
            "display_name": user.display_name or display_name,
            "tier": org.tier,
        }
    await session.commit()  # release the read txn before opening writes

    # CASE A: the email already belongs to a real account → link onto it.
    if found_data is not None:
        async with session.begin():
            await _attach_subject_to(
                session,
                subject=subject,
                identity_id=found_data["identity_id"],
                name=display_name,
                provisional=provisional,
            )
        return ResolvedLogin(
            identity_id=str(found_data["identity_id"]),
            user_id=found_data["user_id"],
            organization_id=found_data["org_id"],
            organization_slug=found_data["org_slug"],
            email=found_data["email"],
            display_name=found_data["display_name"],
            organization_tier=found_data["tier"],
            organization_name=found_data["org_name"],
        )

    # CASE B1: subject already has a placeholder-only account → adopt it by
    # attaching this real, verified email (avoids a duplicate).
    if (
        provisional is not None
        and not provisional.get("orphan")
        and provisional.get("is_placeholder")
        and provisional.get("user_id")
    ):
        async with session.begin():
            ident = await session.get(Identity, provisional["identity_id"])
            if ident is not None:
                ident.primary_email = email
                ident.primary_email_verified = True
            stale_user = await session.get(User, uuid.UUID(provisional["user_id"]))
            if stale_user is not None:
                stale_user.email = email
                if not (stale_user.display_name or "").strip():
                    stale_user.display_name = display_name
        row = (
            await session.execute(
                select(User, Organization)
                .join(Organization, Organization.id == User.organization_id)
                .where(User.id == uuid.UUID(provisional["user_id"]))
                .limit(1)
            )
        ).first()
        await session.commit()
        if row is None:
            raise GoogleLoginError("account adoption failed unexpectedly")
        user, org = row
        return ResolvedLogin(
            identity_id=str(provisional["identity_id"]),
            user_id=str(user.id),
            organization_id=str(org.id),
            organization_slug=org.slug,
            email=email,
            display_name=user.display_name,
            organization_tier=org.tier,
            organization_name=org.name,
        )

    # CASE B2: brand-new → bootstrap a personal account, then link.
    slug = _slug_for(display_name)
    org_name = f"{display_name}'s workspace"
    result_boot = await bootstrap_organization(
        session,
        name=org_name,
        slug=slug,
        owner_email=email,
        owner_display_name=display_name,
        tier="personal",
    )
    async with session.begin():
        identity = await session.get(Identity, result_boot.identity_id)
        if identity is not None:
            identity.primary_email_verified = True
        await _attach_subject_to(
            session,
            subject=subject,
            identity_id=result_boot.identity_id,
            name=display_name,
            provisional=provisional,
        )
    return ResolvedLogin(
        identity_id=str(result_boot.identity_id),
        user_id=str(result_boot.user_id),
        organization_id=str(result_boot.organization_id),
        organization_slug=slug,
        email=email,
        display_name=display_name,
        organization_tier="personal",
        organization_name=org_name,
    )


async def complete_link(
    session: AsyncSession,
    *,
    code: str,
    state: str,
    redirect_uri: str,
) -> None:
    """Attach the consenting Google identity to the account named in state.

    Raises :class:`GoogleLoginError` if the state is bad or the Google
    subject is already linked to a *different* Quberi account.
    """

    payload = _read_state(state, expected_purpose=_PURPOSE_LINK)
    identity_id = payload.get("iid")
    if not identity_id:
        raise GoogleLoginError("link state missing account")

    result = await _token_request(code=code, redirect_uri=redirect_uri)
    info = await _fetch_user_info(result.access_token)
    subject = str(info["sub"])
    display_name = (info.get("name") or info.get("email") or "Google").strip()

    existing = (
        await session.execute(
            select(ExternalIdentity).where(
                ExternalIdentity.provider == _PROVIDER,
                ExternalIdentity.subject == subject,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if str(existing.identity_id) == str(identity_id):
            return  # already linked to this account — idempotent success
        raise GoogleLoginError(
            "this Google account is already linked to another Quberi account"
        )

    await session.commit()
    async with session.begin():
        session.add(
            ExternalIdentity(
                identity_id=uuid.UUID(str(identity_id)),
                provider=_PROVIDER,
                subject=subject,
                attributes={"source": "google_link", "name": display_name},
            )
        )


async def unlink(session: AsyncSession, *, identity_id: uuid.UUID) -> None:
    """Remove the Google link for ``identity_id``, if any (idempotent)."""

    await session.commit()
    async with session.begin():
        existing = (
            await session.execute(
                select(ExternalIdentity).where(
                    ExternalIdentity.provider == _PROVIDER,
                    ExternalIdentity.identity_id == identity_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            await session.delete(existing)


__all__ = [
    "GoogleLoginError",
    "build_login_authorize_url",
    "build_link_authorize_url",
    "state_purpose",
    "state_flow",
    "complete_login",
    "complete_link",
    "unlink",
]
