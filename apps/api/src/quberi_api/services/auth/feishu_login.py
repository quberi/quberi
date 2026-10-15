"""Feishu (Lark) SSO — consent, then *always bind a verified email*.

Unlike the document-source OAuth (which links a Feishu Drive grant to an
*already logged-in* Quberi user), this is authentication: the visitor is
not signed in yet. We take them through Feishu consent and read their
basic profile — but Feishu consent **alone never creates an account**.

To avoid the "same human ends up with two accounts" split, email is the
canonical join key. After Feishu consent:

* If the Feishu subject is already linked to a working account → sign in.
* Otherwise we hand back a short-lived, sealed *bind ticket*. The caller
  must bind a verified email (enter email → email code → verify). Only
  then do we create-or-link the account:
    - email is new  → create a personal account and link Feishu to it;
    - email exists  → link Feishu onto that existing account (no fork).

Reuses the existing Feishu token exchange (``_token_request``) and app
credentials; adds a user-info fetch and the identity mapping.

State (CSRF) and the bind ticket are Fernet-sealed payloads —
self-verifying, so no table or Redis is needed for the handshake.
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
from quberi_api.services.auth import phone_auth as phone_auth_service
from quberi_api.services.auth import verification as verification_service
from quberi_api.services.auth.verification import (
    ResolvedLogin,
    _consume_valid_code,
    _find_active_user,
)
from quberi_api.services.integrations.feishu.oauth import (
    ACCOUNTS_AUTHORIZE_URL,
    OAuthError,
    _token_request,
)
from quberi_api.services.onboarding import bootstrap_organization
from quberi_api.services.security import crypto

_log = logging.getLogger(__name__)

_PROVIDER = "lark"
_PURPOSE_LOGIN = "feishu_login"
_PURPOSE_LINK = "feishu_link"
_PURPOSE_BIND = "feishu_bind"  # verification-code purpose for the email bind
_STATE_TTL_SECONDS = 600
_BIND_TICKET_TTL_SECONDS = 900
_USER_INFO_PATH = "/authen/v1/user_info"
# Synthetic address given to SSO/phone accounts that have no real email yet.
# An account whose email lives in this domain still owes us a verified email.
_PLACEHOLDER_EMAIL_DOMAIN = "@users.quberi.local"


def _is_placeholder_email(email: Optional[str]) -> bool:
    return bool(email) and email.strip().lower().endswith(_PLACEHOLDER_EMAIL_DOMAIN)


async def _identity_has_phone(session: AsyncSession, identity_id: uuid.UUID) -> bool:
    phone = (
        await session.execute(
            select(Identity.primary_phone).where(Identity.id == identity_id)
        )
    ).scalar_one_or_none()
    return bool(phone)


class FeishuLoginError(Exception):
    """Feishu login/link could not be completed."""


@dataclass(frozen=True)
class FeishuLoginOutcome:
    """Result of the Feishu callback.

    Exactly one of the two shapes is populated:

    * ``resolved`` — the Feishu subject already maps to a working account;
      the caller issues a session immediately.
    * ``bind_ticket`` — a fresh Feishu user; the caller must drive the
      email-binding flow (``feishu_name`` / ``feishu_email`` are hints to
      pre-fill the UI).
    """

    resolved: Optional[ResolvedLogin] = None
    bind_ticket: Optional[str] = None
    feishu_name: Optional[str] = None
    feishu_email: Optional[str] = None


# ---------------------------------------------------------------------------
# State (Fernet-sealed nonce; no storage). Carries the purpose (login vs
# link) and, for link, the identity the caller is attaching Feishu to.
# ---------------------------------------------------------------------------


def _seal_state(purpose: str, *, identity_id: Optional[str] = None) -> str:
    payload = {
        "p": purpose,
        "n": secrets.token_urlsafe(16),
        "exp": int(time.time()) + _STATE_TTL_SECONDS,
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
        raise FeishuLoginError("invalid state") from exc
    if not isinstance(payload, dict) or payload.get("p") != expected_purpose:
        raise FeishuLoginError("invalid state")
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or int(exp) < int(time.time()):
        raise FeishuLoginError("state expired; please retry")
    return payload


def state_purpose(state: str) -> str:
    """Return the purpose baked into a state, or raise if it's not ours."""

    try:
        payload = crypto.unseal(state)
    except Exception as exc:  # noqa: BLE001
        raise FeishuLoginError("invalid state") from exc
    purpose = payload.get("p") if isinstance(payload, dict) else None
    if purpose not in (_PURPOSE_LOGIN, _PURPOSE_LINK):
        raise FeishuLoginError("invalid state")
    return purpose


# ---------------------------------------------------------------------------
# Bind ticket (Fernet-sealed; no storage). Carries the authenticated Feishu
# subject + profile hints between the callback and the email-binding step.
# ---------------------------------------------------------------------------


def _seal_bind_ticket(*, subject: str, name: str, email: Optional[str]) -> str:
    payload = {
        "k": "feishu_bind",
        "sub": subject,
        "name": name,
        "email": email or "",
        "exp": int(time.time()) + _BIND_TICKET_TTL_SECONDS,
    }
    sealed = crypto.seal(payload)
    assert sealed is not None  # noqa: S101 — payload is never empty
    return sealed


def _read_bind_ticket(ticket: str) -> dict:
    try:
        payload = crypto.unseal(ticket)
    except Exception as exc:  # noqa: BLE001 — any decode failure = bad ticket
        raise FeishuLoginError("invalid or expired bind ticket") from exc
    if not isinstance(payload, dict) or payload.get("k") != "feishu_bind":
        raise FeishuLoginError("invalid bind ticket")
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or int(exp) < int(time.time()):
        raise FeishuLoginError("bind ticket expired; please sign in with Feishu again")
    if not payload.get("sub"):
        raise FeishuLoginError("invalid bind ticket")
    return payload


# ---------------------------------------------------------------------------
# 1. Authorize URL
# ---------------------------------------------------------------------------


def _authorize_url(*, redirect_uri: str, state: str) -> str:
    settings = get_settings()
    if not settings.feishu_app_id:
        raise FeishuLoginError("Feishu app_id is not configured")
    params = {
        "client_id": settings.feishu_app_id,
        "redirect_uri": redirect_uri,
        "scope": settings.feishu_login_scopes,
        "state": state,
        "response_type": "code",
    }
    return f"{ACCOUNTS_AUTHORIZE_URL}?{urlencode(params)}"


def build_login_authorize_url(*, redirect_uri: str) -> str:
    return _authorize_url(redirect_uri=redirect_uri, state=_seal_state(_PURPOSE_LOGIN))


def build_link_authorize_url(*, redirect_uri: str, identity_id: str) -> str:
    """Consent URL for *linking* Feishu to an already-signed-in account."""

    return _authorize_url(
        redirect_uri=redirect_uri,
        state=_seal_state(_PURPOSE_LINK, identity_id=identity_id),
    )


# ---------------------------------------------------------------------------
# 2. Callback → identity → ResolvedLogin
# ---------------------------------------------------------------------------


async def _fetch_user_info(access_token: str) -> dict:
    """Call Feishu ``authen/v1/user_info`` with the user access token."""

    settings = get_settings()
    url = f"{settings.feishu_base_url}{_USER_INFO_PATH}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        resp = await http.get(
            url, headers={"Authorization": f"Bearer {access_token}"}
        )
    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise FeishuLoginError(
            f"user_info returned non-JSON: {resp.text[:200]}"
        ) from exc
    if payload.get("code") not in (0, None):
        raise FeishuLoginError(
            f"user_info failed [{payload.get('code')}]: "
            f"{payload.get('msg') or payload}"
        )
    data = payload.get("data") or payload
    if not isinstance(data, dict):
        raise FeishuLoginError("user_info returned no data")
    return data


def _subject_of(info: dict) -> str:
    subject = info.get("union_id") or info.get("open_id") or info.get("user_id")
    if not subject:
        raise FeishuLoginError("Feishu profile has no stable id")
    return str(subject)


async def complete_login(
    session: AsyncSession,
    *,
    code: str,
    state: str,
    redirect_uri: str,
) -> FeishuLoginOutcome:
    """Validate state, resolve the Feishu user.

    Returns a :class:`FeishuLoginOutcome`: either an immediate login (the
    subject is already linked to a working account) or a bind ticket that
    the caller must exchange for a verified email. Feishu consent alone
    **never** provisions an account.
    """

    _read_state(state, expected_purpose=_PURPOSE_LOGIN)

    try:
        result = await _token_request(
            grant_type="authorization_code", code=code, redirect_uri=redirect_uri
        )
    except OAuthError as exc:
        raise FeishuLoginError(f"Feishu token exchange failed: {exc}") from exc

    info = await _fetch_user_info(result.access_token)
    subject = _subject_of(info)
    display_name = (
        info.get("name") or info.get("en_name") or "Feishu user"
    ).strip()
    email = (info.get("email") or "").strip().lower() or None

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
        # Sign straight in once the account has a *real* contact — a real
        # email OR a bound phone. Accounts that still carry only a placeholder
        # email and no phone (auto-provisioned by the old flow) must bind a
        # verified contact first.
        if resolved is not None:
            has_phone = await _identity_has_phone(session, ext.identity_id)
            if not _is_placeholder_email(resolved.email) or has_phone:
                return FeishuLoginOutcome(resolved=resolved)
        # Orphan link, or contactless placeholder account → fall through to
        # the bind flow, which repoints/merges the link onto the bound account.

    # New / orphaned / placeholder-only Feishu subject → require email binding.
    await session.commit()  # release any autobegun read txn
    return FeishuLoginOutcome(
        bind_ticket=_seal_bind_ticket(subject=subject, name=display_name, email=email),
        feishu_name=display_name,
        feishu_email=email,
    )


async def _login_existing_identity(
    session: AsyncSession, *, identity_id: uuid.UUID, fallback_name: str
) -> Optional[ResolvedLogin]:
    """Return a ResolvedLogin for an existing identity's account, or None."""

    row = (
        await session.execute(
            select(User, Organization)
            .join(Organization, Organization.id == User.organization_id)
            .where(User.identity_id == identity_id)
            .where(User.status != "archived")
            # Prefer the personal org so returning users land "home".
            .order_by(Organization.tier.desc(), User.created_at.asc())
            .limit(1)
        )
    ).first()
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


# ---------------------------------------------------------------------------
# 3. Email binding (the only path that creates/links a Feishu account)
# ---------------------------------------------------------------------------


async def request_email_code_for_bind(
    session: AsyncSession,
    *,
    ticket: str,
    email: str,
    ip_address: Optional[str] = None,
) -> None:
    """Send a verification code to the email the Feishu user wants to bind.

    The ticket proves the caller just completed Feishu consent (so this
    endpoint can't be used to spam arbitrary inboxes). A code is sent
    whether or not the email already has an account — the verify step then
    links to the existing account or creates a new one.
    """

    _read_bind_ticket(ticket)  # validates / raises on tamper or expiry
    await verification_service.request_code(
        session,
        email=email,
        purpose=_PURPOSE_BIND,
        ip_address=ip_address,
    )


def _slug_for(display_name: str) -> str:
    ascii_name = (
        unicodedata.normalize("NFKD", display_name)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    base = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:40] or "ws"
    return f"{base}-{secrets.token_hex(3)}"


async def _provisional_account_for_subject(
    session: AsyncSession, subject: str
) -> Optional[dict]:
    """Describe the account the Feishu ``subject`` is currently linked to.

    Returns ``None`` if the subject has no link yet. Otherwise a dict with
    the linked account's ids/email plus ``is_placeholder`` (the email is a
    synthetic ``@users.quberi.local`` address) and ``orphan`` (the link
    exists but has no active user). Read-only.
    """

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
            "org_id": None,
            "org_slug": None,
            "email": None,
            "display_name": None,
            "tier": None,
            "is_placeholder": True,
            "orphan": True,
        }
    user, org = row
    return {
        "identity_id": ext.identity_id,
        "user_id": str(user.id),
        "org_id": str(org.id),
        "org_slug": org.slug,
        "org_name": org.name,
        "email": user.email,
        "display_name": user.display_name,
        "tier": org.tier,
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
    """Point the Feishu ``subject`` at ``identity_id`` (call inside a txn).

    * no existing link → insert;
    * already linked here → idempotent no-op;
    * linked to an orphan or placeholder-only account → repoint and archive
      that abandoned account's user (it was never a "real" account);
    * linked to a *real* (email-bound) account → refuse to hijack.
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
                attributes={"source": "feishu_login", "name": name},
            )
        )
        return

    if existing.identity_id == identity_id:
        return  # already linked here — idempotent

    abandonable = provisional is not None and (
        provisional.get("orphan") or provisional.get("is_placeholder")
    )
    if not abandonable:
        raise FeishuLoginError(
            "this Feishu account is already linked to another Quberi account"
        )

    # Repoint the link onto the bound account, then retire the placeholder
    # account it used to point at so it can't be signed into again.
    existing.identity_id = identity_id
    prov_user_id = provisional.get("user_id") if provisional else None
    if prov_user_id:
        stale = await session.get(User, uuid.UUID(prov_user_id))
        if stale is not None:
            stale.status = "archived"


async def bind_email_and_resolve(
    session: AsyncSession, *, ticket: str, email: str, code: str
) -> ResolvedLogin:
    """Verify the email code, then create-or-link the Feishu account.

    Resolution rules (email is the canonical join key, so we never fork):

    * email already maps to a real account → link/merge Feishu onto it;
    * email is new **and** the subject already has a placeholder-only
      account → adopt that account by giving it this real email (no fork);
    * email is new and the subject is brand-new → bootstrap a personal
      account and link Feishu to it.
    """

    payload = _read_bind_ticket(ticket)
    subject = str(payload["sub"])
    feishu_name = str(payload.get("name") or "Feishu user").strip() or "Feishu user"
    email = (email or "").strip().lower()

    # Raises VerificationError on wrong/expired/exhausted code.
    await _consume_valid_code(session, email=email, code=code, purpose=_PURPOSE_BIND)

    # Read current state (autobegins a read txn): does the email already have
    # an account, and is the Feishu subject already (provisionally) linked?
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
            "display_name": user.display_name or feishu_name,
            "tier": org.tier,
        }
    provisional = await _provisional_account_for_subject(session, subject)
    await session.commit()  # release the read txn before opening writes

    # CASE A: the email already belongs to a real account → link onto it.
    if found_data is not None:
        async with session.begin():
            await _attach_subject_to(
                session,
                subject=subject,
                identity_id=found_data["identity_id"],
                name=feishu_name,
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

    # CASE B1: new email, but the subject already has a placeholder-only
    # account → adopt it by attaching this real email (avoids a duplicate).
    if (
        provisional is not None
        and not provisional.get("orphan")
        and provisional.get("is_placeholder")
    ):
        async with session.begin():
            ident = await session.get(Identity, provisional["identity_id"])
            if ident is not None:
                ident.primary_email = email
            stale_user = await session.get(User, uuid.UUID(provisional["user_id"]))
            if stale_user is not None:
                stale_user.email = email
                if not (stale_user.display_name or "").strip():
                    stale_user.display_name = feishu_name
        return ResolvedLogin(
            identity_id=str(provisional["identity_id"]),
            user_id=provisional["user_id"],
            organization_id=provisional["org_id"],
            organization_slug=provisional["org_slug"],
            email=email,
            display_name=provisional["display_name"] or feishu_name,
            organization_tier=provisional["tier"],
            organization_name=provisional["org_name"],
        )

    # CASE B2: brand-new → bootstrap a personal account, then link.
    slug = _slug_for(feishu_name)
    org_name = f"{feishu_name}'s workspace"
    result = await bootstrap_organization(
        session,
        name=org_name,
        slug=slug,
        owner_email=email,
        owner_display_name=feishu_name,
        tier="personal",
    )
    async with session.begin():
        await _attach_subject_to(
            session,
            subject=subject,
            identity_id=result.identity_id,
            name=feishu_name,
            provisional=provisional,
        )
    return ResolvedLogin(
        identity_id=str(result.identity_id),
        user_id=str(result.user_id),
        organization_id=str(result.organization_id),
        organization_slug=slug,
        email=email,
        display_name=feishu_name,
        organization_tier="personal",
        organization_name=org_name,
    )


# ---------------------------------------------------------------------------
# 3b. Phone binding (same idea as email; phone is the join key)
# ---------------------------------------------------------------------------


async def request_phone_code_for_bind(
    session: AsyncSession,
    *,
    ticket: str,
    phone: str,
    ip_address: Optional[str] = None,
) -> None:
    """Send an SMS code to the phone the social user wants to bind."""

    _read_bind_ticket(ticket)  # validates / raises on tamper or expiry
    await phone_auth_service.mint_phone_code(
        session, phone=phone, purpose=_PURPOSE_BIND, ip_address=ip_address
    )


async def bind_phone_and_resolve(
    session: AsyncSession, *, ticket: str, phone: str, code: str
) -> ResolvedLogin:
    """Verify the SMS code, then create-or-link the social account by phone.

    * phone already maps to a real account → link the social subject onto it;
    * phone is new **and** the subject has a placeholder-only account → adopt
      that account by setting its ``primary_phone`` (no fork);
    * phone new and subject brand-new → bootstrap a personal account keyed by
      the phone and link the subject.
    """

    payload = _read_bind_ticket(ticket)
    subject = str(payload["sub"])
    feishu_name = str(payload.get("name") or "Feishu user").strip() or "Feishu user"
    phone = phone_auth_service._normalize_phone(phone)
    if not phone:
        raise FeishuLoginError("phone is required")

    # Raises VerificationError on wrong/expired/exhausted code.
    await _consume_valid_code(session, email=phone, code=code, purpose=_PURPOSE_BIND)

    # Does this phone already belong to a real account? (autobegins read txn)
    phone_identity = (
        await session.execute(
            select(Identity).where(Identity.primary_phone == phone)
        )
    ).scalar_one_or_none()
    found_data = None
    if phone_identity is not None:
        row = (
            await session.execute(
                select(User, Organization)
                .join(Organization, Organization.id == User.organization_id)
                .where(User.identity_id == phone_identity.id)
                .where(User.status != "archived")
                .order_by(Organization.tier.desc(), User.created_at.asc())
                .limit(1)
            )
        ).first()
        if row is not None:
            user, org = row
            found_data = {
                "identity_id": user.identity_id,
                "user_id": str(user.id),
                "org_id": str(org.id),
                "org_slug": org.slug,
                "org_name": org.name,
                "email": user.email,
                "display_name": user.display_name or feishu_name,
                "tier": org.tier,
            }
    provisional = await _provisional_account_for_subject(session, subject)
    await session.commit()  # release the read txn before opening writes

    # CASE A: the phone already belongs to a real account → link onto it.
    if found_data is not None:
        async with session.begin():
            await _attach_subject_to(
                session,
                subject=subject,
                identity_id=found_data["identity_id"],
                name=feishu_name,
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

    # CASE B1: new phone, subject already has a placeholder-only account →
    # adopt it by attaching this phone as the join key (avoids a duplicate).
    if (
        provisional is not None
        and not provisional.get("orphan")
        and provisional.get("is_placeholder")
    ):
        async with session.begin():
            ident = await session.get(Identity, provisional["identity_id"])
            if ident is not None:
                ident.primary_phone = phone
            stale_user = await session.get(User, uuid.UUID(provisional["user_id"]))
            if stale_user is not None and not (stale_user.display_name or "").strip():
                stale_user.display_name = feishu_name
        return ResolvedLogin(
            identity_id=str(provisional["identity_id"]),
            user_id=provisional["user_id"],
            organization_id=provisional["org_id"],
            organization_slug=provisional["org_slug"],
            email=provisional["email"],
            display_name=provisional["display_name"] or feishu_name,
            organization_tier=provisional["tier"],
            organization_name=provisional["org_name"],
        )

    # CASE B2: brand-new → bootstrap a personal account keyed by the phone.
    slug = _slug_for(feishu_name)
    owner_email = phone_auth_service._placeholder_email(phone)
    org_name = f"{feishu_name}'s workspace"
    result = await bootstrap_organization(
        session,
        name=org_name,
        slug=slug,
        owner_email=owner_email,
        owner_display_name=feishu_name,
        tier="personal",
    )
    async with session.begin():
        ident = await session.get(Identity, result.identity_id)
        if ident is not None:
            ident.primary_phone = phone
        await _attach_subject_to(
            session,
            subject=subject,
            identity_id=result.identity_id,
            name=feishu_name,
            provisional=provisional,
        )
    return ResolvedLogin(
        identity_id=str(result.identity_id),
        user_id=str(result.user_id),
        organization_id=str(result.organization_id),
        organization_slug=slug,
        email=owner_email,
        display_name=feishu_name,
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
    """Attach the consenting Feishu identity to the account named in state.

    Raises :class:`FeishuLoginError` if the state is bad or the Feishu
    subject is already linked to a *different* Quberi account.
    """

    payload = _read_state(state, expected_purpose=_PURPOSE_LINK)
    identity_id = payload.get("iid")
    if not identity_id:
        raise FeishuLoginError("link state missing account")

    try:
        result = await _token_request(
            grant_type="authorization_code", code=code, redirect_uri=redirect_uri
        )
    except OAuthError as exc:
        raise FeishuLoginError(f"Feishu token exchange failed: {exc}") from exc

    info = await _fetch_user_info(result.access_token)
    subject = _subject_of(info)
    feishu_name = (info.get("name") or info.get("en_name") or "Feishu").strip()

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
        raise FeishuLoginError(
            "this Feishu account is already linked to another Quberi account"
        )

    await session.commit()
    async with session.begin():
        session.add(
            ExternalIdentity(
                identity_id=uuid.UUID(str(identity_id)),
                provider=_PROVIDER,
                subject=subject,
                attributes={"source": "feishu_link", "name": feishu_name},
            )
        )


__all__ = [
    "build_login_authorize_url",
    "build_link_authorize_url",
    "complete_login",
    "complete_link",
    "request_email_code_for_bind",
    "bind_email_and_resolve",
    "request_phone_code_for_bind",
    "bind_phone_and_resolve",
    "state_purpose",
    "FeishuLoginOutcome",
    "FeishuLoginError",
]
