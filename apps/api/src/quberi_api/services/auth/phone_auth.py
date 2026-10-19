"""Phone verification-code login (login-or-signup in one, like most CN apps).

Enter phone → receive a code → verify. If the phone already maps to an
account we log into it; otherwise we auto-create a personal account keyed
by the phone. The phone number is the join key (unique per identity), so
repeat logins never fork a new account.

Reuses the shared :class:`VerificationCode` machinery (codes stored hashed,
expiry, attempt cap, per-identifier cooldown). SMS delivery goes through
:mod:`sms_sender`, which is a dev-log stub until a real provider is wired.
"""

from __future__ import annotations

import logging
import re
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.core.config import get_settings
from quberi_api.domain.auth.models import VerificationCode
from quberi_api.domain.identity.models import Identity
from quberi_api.services.auth.account_status import (
    best_login_user,
    raise_if_identity_suspended,
)
from quberi_api.services.auth.sms_sender import send_sms
from quberi_api.services.auth.verification import (
    ResolvedLogin,
    VerificationError,
    _consume_valid_code,
    _hash_code,
)
from quberi_api.services.onboarding import bootstrap_organization

_log = logging.getLogger(__name__)

_PURPOSE = "phone_login"
_PLACEHOLDER_DOMAIN = "@users.quberi.local"


class PhoneSignupError(Exception):
    """Phone signup could not be completed (e.g. team signup on an existing phone)."""


def _slug_base(name: str) -> str:
    ascii_name = (
        unicodedata.normalize("NFKD", name or "")
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:40] or "ws"


def _normalize_phone(phone: str) -> str:
    """Keep a leading ``+`` and digits only; strip spaces/dashes/parens."""

    raw = (phone or "").strip()
    if not raw:
        return ""
    plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    return ("+" + digits) if plus else digits


def _placeholder_email(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    return f"phone-{digits}{_PLACEHOLDER_DOMAIN}"


def _display_name(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    tail = digits[-4:] if len(digits) >= 4 else digits
    return f"用户{tail}" if tail else "用户"


async def mint_phone_code(
    session: AsyncSession,
    *,
    phone: str,
    purpose: str = _PURPOSE,
    ip_address: Optional[str] = None,
) -> None:
    """Mint + 'send' an SMS verification code for a phone under ``purpose``.

    Shared by the login/signup flow (``purpose=phone_login``) and the social
    bind flow (a distinct purpose), so codes can't be crossed between flows.
    Per-phone+purpose cooldown, hashed storage, dev logs via the SMS stub.
    """

    phone = _normalize_phone(phone)
    if not phone:
        return
    settings = get_settings()

    # Per-phone+purpose cooldown (anti-spam), same policy as email.
    cooldown = settings.verification_code_cooldown_seconds
    recent = (
        await session.execute(
            select(VerificationCode.created_at)
            .where(func.lower(VerificationCode.email) == phone.lower())
            .where(VerificationCode.purpose == purpose)
            .order_by(VerificationCode.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if recent is not None and (now - recent).total_seconds() < cooldown:
        _log.info("phone code cooldown active for %s; skipping", phone)
        return

    code = f"{secrets.randbelow(1_000_000):06d}"
    expires = now + timedelta(seconds=settings.verification_code_ttl_seconds)
    await session.commit()  # release read txn before writing
    async with session.begin():
        session.add(
            VerificationCode(
                email=phone,  # the column is a generic identifier here
                code_hash=_hash_code(phone, code),
                purpose=purpose,
                expires_at=expires,
                attempts=0,
                ip_address=ip_address,
            )
        )

    minutes = settings.verification_code_ttl_seconds // 60
    text = f"【Quberi】验证码 {code},{minutes} 分钟内有效。"
    await send_sms(to_phone=phone, text=text)


async def request_phone_code(
    session: AsyncSession, *, phone: str, ip_address: Optional[str] = None
) -> None:
    """Mint + 'send' a login/signup verification code for a phone."""

    await mint_phone_code(
        session, phone=phone, purpose=_PURPOSE, ip_address=ip_address
    )


async def _find_identity_by_phone(
    session: AsyncSession, phone: str
) -> Optional[Identity]:
    return (
        await session.execute(
            select(Identity).where(Identity.primary_phone == phone)
        )
    ).scalar_one_or_none()


async def verify_phone_and_resolve(
    session: AsyncSession,
    *,
    phone: str,
    code: str,
    workspace_name: Optional[str] = None,
    display_name: Optional[str] = None,
) -> ResolvedLogin:
    """Verify the code; log into the phone's account, or create one.

    Mirrors the email convention: ``workspace_name`` present means a **team**
    signup (a fresh ``tier=team`` org). Team signup only mints new tenants —
    a phone that already has an account must log in instead. Without a
    workspace name it is the unified personal/login path (unchanged): log into
    the phone's account, or auto-create a personal one.
    """

    phone = _normalize_phone(phone)
    if not phone:
        raise VerificationError("phone and code are required")
    workspace_name = (workspace_name or "").strip()
    display_name = (display_name or "").strip()

    # Raises VerificationError on wrong/expired/exhausted code.
    await _consume_valid_code(session, email=phone, code=code, purpose=_PURPOSE)

    identity = await _find_identity_by_phone(session, phone)

    # Team signup: brand-new phones only (mirror email signup semantics).
    if workspace_name:
        if identity is not None:
            raise PhoneSignupError("该手机号已注册，请直接登录")
        if not display_name:
            raise PhoneSignupError("请填写你的名字")
        return await _provision(
            session,
            phone=phone,
            tier="team",
            workspace_name=workspace_name,
            display_name=display_name,
        )

    # Personal / login (unchanged unified behavior).
    if identity is not None:
        resolved = await _login_existing(session, identity_id=identity.id, phone=phone)
        if resolved is not None:
            return resolved
        # Identity exists but has no usable user/org (rare) → provision.
    return await _provision(
        session, phone=phone, tier="personal", display_name=display_name or None
    )


async def _login_existing(
    session: AsyncSession, *, identity_id, phone: str
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
        display_name=user.display_name,
        organization_tier=org.tier,
        organization_name=org.name,
    )


async def _provision(
    session: AsyncSession,
    *,
    phone: str,
    tier: str = "personal",
    workspace_name: Optional[str] = None,
    display_name: Optional[str] = None,
) -> ResolvedLogin:
    name = (display_name or "").strip() or _display_name(phone)
    owner_email = _placeholder_email(phone)

    if tier == "team":
        org_name = (workspace_name or "").strip() or f"{name}'s workspace"
        slug = f"{_slug_base(workspace_name or name)}-{secrets.token_hex(3)}"
    else:
        org_name = f"{name}'s workspace"
        slug = f"u-{secrets.token_hex(3)}"

    # Release any autobegun read txn before bootstrap opens its own.
    await session.commit()
    result = await bootstrap_organization(
        session,
        name=org_name,
        slug=slug,
        owner_email=owner_email,
        owner_display_name=name,
        tier=tier,
    )

    # Record the phone as the identity's join key.
    async with session.begin():
        identity = await session.get(Identity, result.identity_id)
        if identity is not None:
            identity.primary_phone = phone

    return ResolvedLogin(
        identity_id=str(result.identity_id),
        user_id=str(result.user_id),
        organization_id=str(result.organization_id),
        organization_slug=slug,
        email=owner_email,
        display_name=name,
        organization_tier=tier,
        organization_name=org_name,
    )


__all__ = [
    "request_phone_code",
    "mint_phone_code",
    "verify_phone_and_resolve",
    "PhoneSignupError",
]
