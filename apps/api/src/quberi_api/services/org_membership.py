"""Cross-organisation membership for a signed-in identity.

Unlike ``services/members.py`` (which manages members *within* the
caller's current organisation), this module answers "which organisations
does this identity belong to" and "let this identity create a new (team)
organisation of their own" — the two pieces needed to support one login
acting across multiple orgs (their own personal org, orgs they created,
and orgs they were invited into).
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.identity.models import Organization, User
from quberi_api.services.errors import InvalidInputError, NotFoundError
from quberi_api.services.onboarding import bootstrap_organization


@dataclass(frozen=True)
class MyOrganization:
    organization_id: uuid.UUID
    name: str
    slug: str
    tier: str
    user_id: uuid.UUID
    is_pending: bool  # True while the invite hasn't been accepted (status="invited")


async def list_my_organizations(
    session: AsyncSession, *, identity_id: uuid.UUID
) -> list[MyOrganization]:
    """Every organisation this identity has a (non-archived) membership in.

    Covers both self-created orgs (personal, or a team they founded) and
    orgs they were invited into — membership is a single ``User`` row
    either way, so there's nothing provider-specific to branch on here.
    """

    rows = (
        await session.execute(
            select(User, Organization)
            .join(Organization, Organization.id == User.organization_id)
            .where(User.identity_id == identity_id)
            .where(User.status != "archived")
            .order_by(Organization.created_at.asc())
        )
    ).all()
    await session.commit()
    return [
        MyOrganization(
            organization_id=org.id,
            name=org.name,
            slug=org.slug,
            tier=org.tier,
            user_id=user.id,
            is_pending=user.status == "invited",
        )
        for user, org in rows
    ]


_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")


def _slug_for(name: str) -> str:
    ascii_name = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    )
    base = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:60] or "team"
    import secrets

    return f"{base}-{secrets.token_hex(3)}"


async def create_team_organization(
    session: AsyncSession,
    *,
    identity_id: uuid.UUID,
    owner_email: str,
    owner_display_name: str,
    name: str,
) -> MyOrganization:
    """Create a new ``tier=team`` organisation with the caller as owner.

    Anyone with a session can call this — there is no invite/approval step
    for *creating* a team, only for joining someone else's. The caller
    keeps their existing memberships (personal org, any org they were
    invited into); this just adds one more.
    """

    name = name.strip()
    if not name:
        raise InvalidInputError("team name is required")
    if len(name) > 200:
        raise InvalidInputError("team name is too long")

    slug = _slug_for(name)
    result = await bootstrap_organization(
        session,
        name=name,
        slug=slug,
        owner_email=owner_email,
        owner_display_name=owner_display_name,
        tier="team",
    )
    # bootstrap_organization reuses the Identity row for a matching email,
    # but doesn't take identity_id directly — sanity check we landed on the
    # same identity the caller is authenticated as (should always hold,
    # since owner_email comes from the caller's own account).
    if result.identity_id != identity_id:
        raise InvalidInputError(
            "team owner email does not match the signed-in account"
        )
    return MyOrganization(
        organization_id=result.organization_id,
        name=name,
        slug=slug,
        tier="team",
        user_id=result.user_id,
        is_pending=False,
    )


async def resolve_switch_target(
    session: AsyncSession, *, identity_id: uuid.UUID, organization_id: uuid.UUID
) -> User:
    """Return the caller's ``User`` row in ``organization_id``, if a member.

    Raises :class:`NotFoundError` if the identity has no (non-archived)
    membership there — this is the authorization check for switching: you
    can only switch into an org you already belong to.
    """

    user = (
        await session.execute(
            select(User).where(
                User.identity_id == identity_id,
                User.organization_id == organization_id,
                User.status != "archived",
            )
        )
    ).scalar_one_or_none()
    await session.commit()
    if user is None:
        raise NotFoundError("you are not a member of that organization")
    return user


__all__ = [
    "MyOrganization",
    "list_my_organizations",
    "create_team_organization",
    "resolve_switch_target",
]
