"""One-time verification-code flow for email (login-or-signup in one, like
the phone and social login paths).

Enter email → receive a code → verify. If the email already maps to an
account we log into it; otherwise we auto-create a personal account keyed
by the email — same convention as ``phone_auth.py``, ``google_login.py``,
and ``feishu_login.py``. Anyone with a working email can get in; who gets
*invited into an existing team* is a separate, later concern.

Codes are stored hashed, expire quickly, cap attempts, and are
rate-limited per email.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.core.config import get_settings
from quberi_api.domain.auth.models import VerificationCode
from quberi_api.domain.identity.models import Organization, User
from quberi_api.services.auth import email_settings as email_settings_service
from quberi_api.services.auth.account_status import (
    ACCOUNT_UNAVAILABLE_MESSAGE,
    AccountUnavailableError,
    login_usable,
)
from quberi_api.services.auth.email_sender import (
    EmailSendError,
    send_email_with_failover,
)
from quberi_api.services.onboarding import bootstrap_organization

_log = logging.getLogger(__name__)


class VerificationError(Exception):
    """Code verification failed (wrong / expired / too many attempts)."""


@dataclass(frozen=True)
class ResolvedLogin:
    identity_id: str
    user_id: str
    organization_id: str
    organization_slug: str
    email: str
    display_name: str
    organization_tier: str = "team"
    organization_name: str = ""


def _normalize(email: str) -> str:
    return (email or "").strip().lower()


def _hash_code(email: str, code: str) -> str:
    return hashlib.sha256(f"{email}:{code}".encode("utf-8")).hexdigest()


async def _find_active_user(
    session: AsyncSession, email: str
) -> Optional[tuple[User, Organization]]:
    """The account this verified email may sign in to, or ``None`` for a new email.

    A code proves control of the address and nothing more, so only a row whose own
    email is that address can be signed in. The row's identity is never consulted: a
    recycled mailbox must not lead to another account of the previous owner, and a
    social login that reuses this lookup must not get a new subject linked to it.
    If the only matching rows are suspended, the sign-in is refused instead of
    falling through to creating a fresh account.
    """

    row = (
        await session.execute(
            select(User, Organization)
            .join(Organization, Organization.id == User.organization_id)
            .where(func.lower(User.email) == email)
            .where(login_usable())
            .order_by(User.created_at.asc())
            .limit(1)
        )
    ).first()
    if row is not None:
        return (row[0], row[1])
    suspended = (
        await session.execute(
            select(User.id)
            .where(func.lower(User.email) == email, User.status == "suspended")
            .limit(1)
        )
    ).first()
    if suspended is not None:
        raise AccountUnavailableError(ACCOUNT_UNAVAILABLE_MESSAGE)
    return None


async def request_code(
    session: AsyncSession,
    *,
    email: str,
    purpose: str = "login",
    ip_address: Optional[str] = None,
) -> None:
    """Generate + email a verification code.

    Login-or-signup: a code is sent to any well-formed email, whether or
    not it already has an account — ``verify_code`` decides at that point
    whether to log in or auto-provision. Cooldown / rate-limiting still
    applies. Always returns ``None`` (the API responds generically).
    """

    email = _normalize(email)
    if not email:
        return

    settings = get_settings()

    # Per-email cooldown: refuse to mint a new code if the most recent one
    # was created within the cooldown window (anti-spam / email-bomb).
    cooldown = settings.verification_code_cooldown_seconds
    recent = (
        await session.execute(
            select(VerificationCode.created_at)
            .where(func.lower(VerificationCode.email) == email)
            .where(VerificationCode.purpose == purpose)
            .order_by(VerificationCode.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if recent is not None and (now - recent).total_seconds() < cooldown:
        _log.info("verification cooldown active for %s; skipping send", email)
        return

    code = f"{secrets.randbelow(1_000_000):06d}"
    expires = now + timedelta(seconds=settings.verification_code_ttl_seconds)

    await session.commit()  # release the read txn before writing
    async with session.begin():
        session.add(
            VerificationCode(
                email=email,
                code_hash=_hash_code(email, code),
                purpose=purpose,
                expires_at=expires,
                attempts=0,
                ip_address=ip_address,
            )
        )

    # In non-production, log the code so we can test without inbox access.
    if settings.environment != "production":
        _log.warning("DEV verification code for %s: %s", email, code)

    configs = await email_settings_service.enabled_smtp_configs(session)
    if not configs:
        _log.error("no SMTP configured; cannot send verification code to %s", email)
        return
    minutes = settings.verification_code_ttl_seconds // 60
    subject = "Quberi 登录验证码 / Verification code"
    body = (
        f"你的 Quberi 登录验证码是：{code}\n"
        f"{minutes} 分钟内有效。若非本人操作，请忽略本邮件。\n\n"
        f"Your Quberi verification code is: {code}\n"
        f"It expires in {minutes} minutes. Ignore this email if it wasn't you."
    )
    try:
        await send_email_with_failover(
            configs, to_address=email, subject=subject, body_text=body
        )
    except EmailSendError:
        # All providers failed; already logged. Don't leak specifics.
        _log.error("verification email delivery failed for %s (all providers)", email)


async def _consume_valid_code(
    session: AsyncSession,
    *,
    email: str,
    code: str,
    purpose: str,
) -> None:
    """Validate the latest active code for ``email``/``purpose`` and consume it.

    Checks existence, expiry, and attempt count. Records the attempt and burns
    the code on a correct match (or on attempt exhaustion). Raises
    :class:`VerificationError` on any failure. Does no user resolution — that is
    the caller's job (login resolves an existing user; signup creates one).
    """

    email = _normalize(email)
    code = (code or "").strip()
    if not email or not code:
        raise VerificationError("email and code are required")

    settings = get_settings()
    now = datetime.now(timezone.utc)

    row = (
        await session.execute(
            select(VerificationCode)
            .where(func.lower(VerificationCode.email) == email)
            .where(VerificationCode.purpose == purpose)
            .where(VerificationCode.consumed_at.is_(None))
            .order_by(VerificationCode.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if row is None:
        raise VerificationError("no active code; request a new one")

    # Expiry check.
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise VerificationError("code expired; request a new one")

    matches = secrets.compare_digest(row.code_hash, _hash_code(email, code))

    # Record the attempt (and consume on success / lock on exhaustion).
    await session.commit()
    async with session.begin():
        fresh = await session.get(VerificationCode, row.id)
        if fresh is None:
            raise VerificationError("no active code; request a new one")
        fresh.attempts = fresh.attempts + 1
        exhausted = fresh.attempts >= settings.verification_code_max_attempts
        if matches:
            fresh.consumed_at = now
        elif exhausted:
            # Burn the code so a locked-out attacker must request a new one.
            fresh.consumed_at = now

    if not matches:
        raise VerificationError("incorrect code")


def _slug_base(name: str) -> str:
    ascii_name = (
        unicodedata.normalize("NFKD", name or "")
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:40] or "ws"


async def verify_code(
    session: AsyncSession,
    *,
    email: str,
    code: str,
    purpose: str = "login",
    display_name: Optional[str] = None,
) -> ResolvedLogin:
    """Check the code, then log into the email's account or create one.

    Login-or-signup, same convention as phone/Google/Feishu: an email that
    already has an account signs in; a new email gets a personal account
    auto-provisioned on the spot. ``display_name`` seeds the new account's
    name when provisioning (ignored when logging into an existing one).
    """

    email = _normalize(email)
    await _consume_valid_code(session, email=email, code=code, purpose=purpose)

    found = await _find_active_user(session, email)
    if found is not None:
        user, org = found
        return ResolvedLogin(
            identity_id=str(user.identity_id),
            user_id=str(user.id),
            organization_id=str(org.id),
            organization_slug=org.slug,
            email=user.email,
            display_name=user.display_name,
            organization_tier=org.tier,
            organization_name=org.name,
        )

    # Brand-new email → auto-provision a personal account (mirrors
    # phone_auth._provision / google_login's brand-new-subject branch).
    name = (display_name or "").strip() or email.split("@", 1)[0]
    slug = f"{_slug_base(name)}-{secrets.token_hex(3)}"
    org_name = f"{name}'s workspace"
    result = await bootstrap_organization(
        session,
        name=org_name,
        slug=slug,
        owner_email=email,
        owner_display_name=name,
        tier="personal",
    )
    return ResolvedLogin(
        identity_id=str(result.identity_id),
        user_id=str(result.user_id),
        organization_id=str(result.organization_id),
        organization_slug=slug,
        email=email,
        display_name=name,
        organization_tier="personal",
        organization_name=org_name,
    )
