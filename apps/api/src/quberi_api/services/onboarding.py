"""Organisation & first-user onboarding.

``bootstrap_organization`` creates the full "day zero" state for a new tenant
in a single transaction:

1. ``Organization`` — the tenant boundary.
2. ``Identity`` — reused if one already exists for the owner's email so the
   same human can hold ownership across multiple tenants later.
3. ``User`` — the org-scoped membership row for the owner.
4. ``Space`` — a personal space for the owner, ``preset='personal'``,
   trust_level ``trusted``, visibility ``private``.
5. ``RoleBinding`` — grants the seeded ``owner`` system role to the user on
   the organisation resource.
6. ``AuditEvent`` — an ``organization.bootstrapped`` row so the trail exists
   from t=0.

The whole flow is atomic: any DB error (unique-violation, missing seed data,
etc.) rolls the whole thing back so we never end up with a half-provisioned
tenant.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.authz.models import Role, RoleBinding
from quberi_api.domain.identity.models import Identity, Organization, User
from quberi_api.domain.spaces.models import Space
from quberi_api.services.errors import (
    AlreadyExistsError,
    InvalidInputError,
    NotFoundError,
)

# ---------------------------------------------------------------------------
# Result payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BootstrapResult:
    organization_id: uuid.UUID
    identity_id: uuid.UUID
    user_id: uuid.UUID
    personal_space_id: uuid.UUID
    role_binding_id: uuid.UUID


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_slug(slug: str) -> None:
    if not _SLUG_RE.match(slug):
        raise InvalidInputError(
            "slug must be 1-80 chars, lowercase alphanumerics with internal hyphens"
        )


def _validate_email(email: str) -> None:
    if not _EMAIL_RE.match(email):
        raise InvalidInputError(f"invalid email: {email!r}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def bootstrap_organization(
    session: AsyncSession,
    *,
    name: str,
    slug: str,
    owner_email: str,
    owner_display_name: str,
    tier: str = "team",
    default_locale: str = "en-US",
    timezone: str = "UTC",
    data_region: str = "local",
) -> BootstrapResult:
    """Create a new organisation and its first owner in one transaction.

    ``tier`` is ``team`` (default, enterprise-shaped) or ``personal``
    (self-serve single-member org whose UI hides organisation concepts).
    """

    name = name.strip()
    slug = slug.strip().lower()
    owner_email = owner_email.strip().lower()
    owner_display_name = owner_display_name.strip()

    if not name:
        raise InvalidInputError("organisation name is required")
    if not owner_display_name:
        raise InvalidInputError("owner display name is required")
    if tier not in ("personal", "team"):
        raise InvalidInputError(f"invalid tier: {tier!r}")
    _validate_slug(slug)
    _validate_email(owner_email)

    # We deliberately open a nested-safe transaction: if the caller already
    # started one we join it, otherwise we open a new one. ``session.begin``
    # gives us that behaviour on SQLAlchemy 2.
    async with session.begin():
        # 1. Locate the owner role BEFORE creating anything so a missing seed
        #    fails fast rather than half-way through.
        owner_role = await _load_system_role(session, "owner")

        # 2. Organisation.
        if await _organization_slug_exists(session, slug):
            raise AlreadyExistsError(f"organisation slug already exists: {slug!r}")

        organization = Organization(
            name=name,
            slug=slug,
            status="active",
            tier=tier,
            default_locale=default_locale,
            timezone=timezone,
            data_region=data_region,
            attributes={},
        )
        session.add(organization)
        await session.flush()

        # 3. Identity (create-or-reuse).
        identity = await _get_or_create_identity(session, email=owner_email)

        # 4. User (org-scoped membership).
        user = User(
            organization_id=organization.id,
            identity_id=identity.id,
            display_name=owner_display_name,
            email=owner_email,
            locale=default_locale,
            timezone=timezone,
            status="active",
            attributes={},
        )
        session.add(user)
        try:
            await session.flush()
        except IntegrityError as exc:
            # Only realistic path here is the identity already being a member
            # of this org — but we just created the org so this shouldn't fire.
            # Guard anyway to keep the message honest.
            raise AlreadyExistsError(
                "owner identity is already a member of this organisation"
            ) from exc

        # 5. Personal Space.
        personal_space = Space(
            organization_id=organization.id,
            name=f"{owner_display_name}'s space",
            # Slug must be unique per organisation. User IDs are UUID4 so a
            # 12-char hex prefix is unique-enough for personal spaces and
            # gives us a stable, human-scannable identifier.
            slug=f"personal-{user.id.hex[:12]}",
            description="Personal workspace for private drafts and experiments.",
            preset="personal",
            owner_type="user",
            owner_id=user.id,
            visibility="private",
            trust_level="trusted",
            inheritance_policy={},
            attributes={},
            status="active",
        )
        session.add(personal_space)
        await session.flush()

        # 6. Owner RoleBinding on the organisation.
        binding = RoleBinding(
            organization_id=organization.id,
            role_id=owner_role.id,
            subject_type="user",
            subject_id=user.id,
            resource_type="organization",
            resource_id=organization.id,
            conditions={},
        )
        session.add(binding)
        await session.flush()

        # 7. Audit trail.
        session.add(
            AuditEvent(
                organization_id=organization.id,
                actor_type="system",
                actor_id=None,
                action="organization.bootstrapped",
                resource_type="organization",
                resource_id=organization.id,
                space_id=personal_space.id,
                payload={
                    "owner_user_id": str(user.id),
                    "owner_email": owner_email,
                    "owner_role": "owner",
                    "personal_space_id": str(personal_space.id),
                },
            )
        )

    return BootstrapResult(
        organization_id=organization.id,
        identity_id=identity.id,
        user_id=user.id,
        personal_space_id=personal_space.id,
        role_binding_id=binding.id,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _load_system_role(session: AsyncSession, code: str) -> Role:
    # ``LIMIT 1`` + ``first()`` — see the members.py comment for rationale.
    stmt = (
        select(Role)
        .where(
            Role.code == code,
            Role.is_system.is_(True),
            Role.organization_id.is_(None),
        )
        .order_by(Role.created_at.asc())
        .limit(1)
    )
    role: Optional[Role] = (await session.execute(stmt)).scalars().first()
    if role is None:
        raise NotFoundError(
            f"system role {code!r} not found — did migration 0002 run?"
        )
    return role


async def _organization_slug_exists(session: AsyncSession, slug: str) -> bool:
    stmt = select(Organization.id).where(Organization.slug == slug)
    return (await session.execute(stmt)).first() is not None


async def _get_or_create_identity(session: AsyncSession, *, email: str) -> Identity:
    stmt = select(Identity).where(Identity.primary_email == email)
    existing: Optional[Identity] = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing

    identity = Identity(
        primary_email=email,
        primary_email_verified=False,
        status="active",
    )
    session.add(identity)
    await session.flush()
    return identity
