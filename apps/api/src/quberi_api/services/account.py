"""Personal account: profile + linked login methods.

A Quberi account is one global :class:`Identity` that can carry several
login methods:

* **email** — the identity's ``primary_email`` (real + verified, not the
  ``…@users.quberi.local`` placeholder minted for email-less SSO signups).
* **lark** — a linked Feishu :class:`ExternalIdentity`.

This service backs the account page: read the current profile + linked
methods, bind/verify a real email, and (via the Feishu login service) link
a Feishu identity to the signed-in account.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.identity.models import ExternalIdentity, Identity, User
from quberi_api.services.auth import phone_auth as phone_auth_service
from quberi_api.services.auth import verification as verification_service
from quberi_api.services.errors import AlreadyExistsError, InvalidInputError, NotFoundError

# Placeholder email domain used when an SSO signup had no real email.
PLACEHOLDER_EMAIL_SUFFIX = "@users.quberi.local"
_BIND_PURPOSE = "bind_email"
_BIND_PHONE_PURPOSE = "bind_phone"


@dataclass(frozen=True)
class AccountView:
    display_name: str
    avatar_url: Optional[str]
    email: str
    email_is_placeholder: bool
    email_verified: bool
    has_email_login: bool
    phone: Optional[str]  # the identity's bound phone, if any
    has_phone: bool
    feishu_linked: bool
    feishu_account: Optional[str]  # the linked Feishu display name, if any
    google_linked: bool
    google_account: Optional[str]  # the linked Google display name, if any


def _is_placeholder(email: str) -> bool:
    return email.endswith(PLACEHOLDER_EMAIL_SUFFIX)


async def get_account(
    session: AsyncSession, *, user_id: uuid.UUID, identity_id: uuid.UUID
) -> AccountView:
    user = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    identity = (
        await session.execute(select(Identity).where(Identity.id == identity_id))
    ).scalar_one()
    feishu = (
        await session.execute(
            select(ExternalIdentity).where(
                ExternalIdentity.identity_id == identity_id,
                ExternalIdentity.provider == "lark",
            )
        )
    ).scalar_one_or_none()
    google = (
        await session.execute(
            select(ExternalIdentity).where(
                ExternalIdentity.identity_id == identity_id,
                ExternalIdentity.provider == "google",
            )
        )
    ).scalar_one_or_none()
    placeholder = _is_placeholder(identity.primary_email)
    feishu_account = None
    if feishu is not None:
        feishu_account = (feishu.attributes or {}).get("name") or "Feishu"
    google_account = None
    if google is not None:
        google_account = (google.attributes or {}).get("name") or "Google"
    return AccountView(
        display_name=user.display_name,
        avatar_url=user.avatar_url,
        email=identity.primary_email,
        email_is_placeholder=placeholder,
        email_verified=identity.primary_email_verified,
        # A usable email login exists once the email is real (not a placeholder).
        has_email_login=not placeholder,
        phone=identity.primary_phone,
        has_phone=bool(identity.primary_phone),
        feishu_linked=feishu is not None,
        feishu_account=feishu_account,
        google_linked=google is not None,
        google_account=google_account,
    )


async def set_display_name(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    display_name: str,
) -> None:
    name = (display_name or "").strip()
    if not name:
        raise InvalidInputError("name is required")
    if len(name) > 120:
        raise InvalidInputError("name is too long")
    await session.commit()
    async with session.begin():
        user = await session.get(User, user_id)
        if user is None or user.organization_id != organization_id:
            raise NotFoundError("user not found")
        user.display_name = name


async def set_avatar_url(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    avatar_url: str,
) -> None:
    await session.commit()
    async with session.begin():
        user = await session.get(User, user_id)
        if user is None or user.organization_id != organization_id:
            raise NotFoundError("user not found")
        user.avatar_url = avatar_url


async def _email_taken_by_other(
    session: AsyncSession, *, email: str, identity_id: uuid.UUID
) -> bool:
    row = (
        await session.execute(
            select(Identity.id).where(
                func.lower(Identity.primary_email) == email,
                Identity.id != identity_id,
            )
        )
    ).first()
    return row is not None


async def request_email_bind(
    session: AsyncSession,
    *,
    identity_id: uuid.UUID,
    new_email: str,
    ip_address: Optional[str] = None,
) -> None:
    """Send a verification code to ``new_email`` to bind it to this account."""

    email = verification_service._normalize(new_email)
    if not email:
        raise InvalidInputError("email is required")
    if await _email_taken_by_other(session, email=email, identity_id=identity_id):
        raise AlreadyExistsError("that email is already used by another account")
    await session.commit()  # release read txn before verification writes
    # Send to any address (this isn't invite-first); proves ownership.
    await verification_service.request_code(
        session,
        email=email,
        purpose=_BIND_PURPOSE,
        ip_address=ip_address,
    )


async def confirm_email_bind(
    session: AsyncSession,
    *,
    identity_id: uuid.UUID,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    new_email: str,
    code: str,
) -> str:
    """Verify the code and set the account's email. Returns the new email."""

    email = verification_service._normalize(new_email)
    # Raises VerificationError on bad/expired code.
    await verification_service._consume_valid_code(
        session, email=email, code=code, purpose=_BIND_PURPOSE
    )
    # Re-check uniqueness at commit time (someone may have claimed it).
    if await _email_taken_by_other(session, email=email, identity_id=identity_id):
        raise AlreadyExistsError("that email is already used by another account")

    await session.commit()
    async with session.begin():
        identity = await session.get(Identity, identity_id)
        if identity is None:
            raise NotFoundError("identity not found")
        identity.primary_email = email
        identity.primary_email_verified = True
        user = await session.get(User, user_id)
        if user is not None and user.organization_id == organization_id:
            user.email = email
    return email


async def _phone_taken_by_other(
    session: AsyncSession, *, phone: str, identity_id: uuid.UUID
) -> bool:
    row = (
        await session.execute(
            select(Identity.id).where(
                Identity.primary_phone == phone,
                Identity.id != identity_id,
            )
        )
    ).first()
    return row is not None


async def request_phone_bind(
    session: AsyncSession,
    *,
    identity_id: uuid.UUID,
    new_phone: str,
    ip_address: Optional[str] = None,
) -> None:
    """Send an SMS code to ``new_phone`` to bind it to this account.

    Option-A anti-duplication: if the phone already belongs to another
    account we block outright (no auto-merge) so the user can't fork.
    """

    phone = phone_auth_service._normalize_phone(new_phone)
    if not phone:
        raise InvalidInputError("phone is required")
    if await _phone_taken_by_other(session, phone=phone, identity_id=identity_id):
        raise AlreadyExistsError("that phone is already used by another account")
    await session.commit()  # release read txn before verification writes
    await phone_auth_service.mint_phone_code(
        session, phone=phone, purpose=_BIND_PHONE_PURPOSE, ip_address=ip_address
    )


async def confirm_phone_bind(
    session: AsyncSession,
    *,
    identity_id: uuid.UUID,
    new_phone: str,
    code: str,
) -> str:
    """Verify the SMS code and set the account's phone. Returns the phone."""

    phone = phone_auth_service._normalize_phone(new_phone)
    if not phone:
        raise InvalidInputError("phone is required")
    # Raises VerificationError on bad/expired code.
    await verification_service._consume_valid_code(
        session, email=phone, code=code, purpose=_BIND_PHONE_PURPOSE
    )
    # Re-check uniqueness at commit time (someone may have claimed it).
    if await _phone_taken_by_other(session, phone=phone, identity_id=identity_id):
        raise AlreadyExistsError("that phone is already used by another account")

    await session.commit()
    async with session.begin():
        identity = await session.get(Identity, identity_id)
        if identity is None:
            raise NotFoundError("identity not found")
        identity.primary_phone = phone
    return phone


async def unlink_google(session: AsyncSession, *, identity_id: uuid.UUID) -> None:
    """Unlink Google from this account.

    Refuses if it's the only login method (a real email login always
    counts as a fallback, so this only blocks a placeholder-email account
    whose sole way in was Google).
    """

    identity = (
        await session.execute(select(Identity).where(Identity.id == identity_id))
    ).scalar_one_or_none()
    if identity is None:
        raise NotFoundError("identity not found")
    if _is_placeholder(identity.primary_email) and not identity.primary_phone:
        raise InvalidInputError(
            "cannot unlink Google — set a real email first so you can still sign in"
        )

    from quberi_api.services.auth import google_login as google_login_service

    await google_login_service.unlink(session, identity_id=identity_id)


__all__ = [
    "AccountView",
    "get_account",
    "request_email_bind",
    "confirm_email_bind",
    "request_phone_bind",
    "confirm_phone_bind",
    "unlink_google",
    "PLACEHOLDER_EMAIL_SUFFIX",
]
