"""Platform-wide usage stats for the ops/admin dashboard.

Everything here is read-only and deliberately cheap (simple ``COUNT`` /
``GROUP BY`` queries against indexed columns) — this is a small internal
tool, not an analytics product. All queries exclude the seeded demo
organisation (see ``services.demo_seed``) so platform-admin numbers reflect
real usage, not the always-on sample tenant.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.ai.models import AiProviderBinding
from quberi_api.domain.conversations.models import Conversation, Message
from quberi_api.domain.documents.models import Document
from quberi_api.domain.identity.models import Identity, Organization, User
from quberi_api.domain.spaces.models import Space
from quberi_api.services.demo_seed import DEMO_ORG_SLUG


@dataclass(frozen=True)
class PlatformTotals:
    organizations: int
    organizations_personal: int
    organizations_team: int
    users: int
    identities: int
    spaces: int
    documents: int
    documents_indexed: int
    conversations: int
    messages: int


@dataclass(frozen=True)
class SignupPoint:
    """One day's worth of new organisations + new identities."""

    day: str  # ISO date, UTC
    organizations: int
    identities: int


@dataclass(frozen=True)
class PlatformOverview:
    totals: PlatformTotals
    new_organizations_24h: int
    new_organizations_7d: int
    new_identities_24h: int
    new_identities_7d: int
    active_organizations_7d: int  # orgs with >=1 message in the last 7 days
    signups_by_day: list[SignupPoint]  # last 14 days, oldest first


async def _demo_org_id(session: AsyncSession) -> uuid.UUID | None:
    row = (
        await session.execute(
            select(Organization.id).where(Organization.slug == DEMO_ORG_SLUG)
        )
    ).scalar_one_or_none()
    return row


async def get_overview(session: AsyncSession) -> PlatformOverview:
    demo_id = await _demo_org_id(session)
    org_filter = (Organization.id != demo_id) if demo_id else True

    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    orgs_total = (
        await session.execute(
            select(func.count()).select_from(Organization).where(org_filter)
        )
    ).scalar_one()
    orgs_personal = (
        await session.execute(
            select(func.count())
            .select_from(Organization)
            .where(org_filter, Organization.tier == "personal")
        )
    ).scalar_one()
    orgs_team = (
        await session.execute(
            select(func.count())
            .select_from(Organization)
            .where(org_filter, Organization.tier == "team")
        )
    ).scalar_one()

    # Identities aren't org-scoped, but every real signup has a User row in
    # a non-demo org — join through Users to exclude the demo seed identity.
    user_org_filter = (User.organization_id != demo_id) if demo_id else True
    identities_total = (
        await session.execute(
            select(func.count(func.distinct(Identity.id)))
            .select_from(Identity)
            .join(User, User.identity_id == Identity.id)
            .where(user_org_filter)
        )
    ).scalar_one()

    users_total = (
        await session.execute(
            select(func.count()).select_from(User).where(user_org_filter)
        )
    ).scalar_one()

    space_org_filter = (Space.organization_id != demo_id) if demo_id else True
    spaces_total = (
        await session.execute(
            select(func.count()).select_from(Space).where(space_org_filter)
        )
    ).scalar_one()

    doc_org_filter = (Document.organization_id != demo_id) if demo_id else True
    documents_total = (
        await session.execute(
            select(func.count()).select_from(Document).where(doc_org_filter)
        )
    ).scalar_one()
    documents_indexed = (
        await session.execute(
            select(func.count())
            .select_from(Document)
            .where(doc_org_filter, Document.indexing_state == "indexed")
        )
    ).scalar_one()

    conv_org_filter = (Conversation.organization_id != demo_id) if demo_id else True
    conversations_total = (
        await session.execute(
            select(func.count()).select_from(Conversation).where(conv_org_filter)
        )
    ).scalar_one()

    msg_org_filter = (Message.organization_id != demo_id) if demo_id else True
    messages_total = (
        await session.execute(
            select(func.count()).select_from(Message).where(msg_org_filter)
        )
    ).scalar_one()

    new_orgs_24h = (
        await session.execute(
            select(func.count())
            .select_from(Organization)
            .where(org_filter, Organization.created_at >= day_ago)
        )
    ).scalar_one()
    new_orgs_7d = (
        await session.execute(
            select(func.count())
            .select_from(Organization)
            .where(org_filter, Organization.created_at >= week_ago)
        )
    ).scalar_one()

    new_identities_24h = (
        await session.execute(
            select(func.count(func.distinct(Identity.id)))
            .select_from(Identity)
            .join(User, User.identity_id == Identity.id)
            .where(user_org_filter, Identity.created_at >= day_ago)
        )
    ).scalar_one()
    new_identities_7d = (
        await session.execute(
            select(func.count(func.distinct(Identity.id)))
            .select_from(Identity)
            .join(User, User.identity_id == Identity.id)
            .where(user_org_filter, Identity.created_at >= week_ago)
        )
    ).scalar_one()

    active_orgs_7d = (
        await session.execute(
            select(func.count(func.distinct(Message.organization_id)))
            .select_from(Message)
            .where(msg_org_filter, Message.created_at >= week_ago)
        )
    ).scalar_one()

    # 14-day signup trend, bucketed by UTC day. Two separate GROUP BYs
    # (orgs created, identities created) merged client-side into one row
    # per day — simplest to read, and these tables are small.
    fourteen_days_ago = now - timedelta(days=14)
    org_day_col = func.date_trunc("day", Organization.created_at)
    org_rows = (
        await session.execute(
            select(org_day_col, func.count())
            .select_from(Organization)
            .where(org_filter, Organization.created_at >= fourteen_days_ago)
            .group_by(org_day_col)
        )
    ).all()
    ident_day_col = func.date_trunc("day", Identity.created_at)
    ident_rows = (
        await session.execute(
            select(ident_day_col, func.count(func.distinct(Identity.id)))
            .select_from(Identity)
            .join(User, User.identity_id == Identity.id)
            .where(user_org_filter, Identity.created_at >= fourteen_days_ago)
            .group_by(ident_day_col)
        )
    ).all()

    orgs_by_day = {d.date().isoformat(): c for d, c in org_rows}
    idents_by_day = {d.date().isoformat(): c for d, c in ident_rows}
    days = [
        (now - timedelta(days=offset)).date().isoformat() for offset in range(13, -1, -1)
    ]
    signups_by_day = [
        SignupPoint(
            day=day,
            organizations=orgs_by_day.get(day, 0),
            identities=idents_by_day.get(day, 0),
        )
        for day in days
    ]

    return PlatformOverview(
        totals=PlatformTotals(
            organizations=orgs_total,
            organizations_personal=orgs_personal,
            organizations_team=orgs_team,
            users=users_total,
            identities=identities_total,
            spaces=spaces_total,
            documents=documents_total,
            documents_indexed=documents_indexed,
            conversations=conversations_total,
            messages=messages_total,
        ),
        new_organizations_24h=new_orgs_24h,
        new_organizations_7d=new_orgs_7d,
        new_identities_24h=new_identities_24h,
        new_identities_7d=new_identities_7d,
        active_organizations_7d=active_orgs_7d,
        signups_by_day=signups_by_day,
    )


@dataclass(frozen=True)
class OrganizationRow:
    id: uuid.UUID
    name: str
    slug: str
    tier: str
    status: str
    created_at: datetime
    member_count: int
    space_count: int
    document_count: int
    conversation_count: int
    message_count: int
    owner_email: Optional[str]
    is_demo: bool


async def list_organizations_detailed(
    session: AsyncSession, *, limit: int = 50, offset: int = 0
) -> tuple[list[OrganizationRow], int]:
    """Org list with per-org activity counts, newest first.

    Four cheap scalar subqueries per row via correlated subqueries would be
    slow at real scale; for an internal ops tool with a handful of tenants
    this is simpler to read than a hand-rolled multi-join aggregate, and
    plenty fast. Revisit if the org count grows past a few thousand.
    """

    demo_id = await _demo_org_id(session)

    total = (await session.execute(select(func.count()).select_from(Organization))).scalar_one()

    orgs = (
        await session.execute(
            select(Organization)
            .order_by(Organization.created_at.desc(), Organization.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    rows: list[OrganizationRow] = []
    for org in orgs:
        member_count = (
            await session.execute(
                select(func.count())
                .select_from(User)
                .where(User.organization_id == org.id)
            )
        ).scalar_one()
        space_count = (
            await session.execute(
                select(func.count())
                .select_from(Space)
                .where(Space.organization_id == org.id)
            )
        ).scalar_one()
        document_count = (
            await session.execute(
                select(func.count())
                .select_from(Document)
                .where(Document.organization_id == org.id)
            )
        ).scalar_one()
        conversation_count = (
            await session.execute(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.organization_id == org.id)
            )
        ).scalar_one()
        message_count = (
            await session.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.organization_id == org.id)
            )
        ).scalar_one()
        # First-created user in the org is a reasonable "owner" proxy for
        # display purposes — good enough for an ops list, not a security
        # decision.
        owner_row = (
            await session.execute(
                select(User.email)
                .where(User.organization_id == org.id)
                .order_by(User.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()

        rows.append(
            OrganizationRow(
                id=org.id,
                name=org.name,
                slug=org.slug,
                tier=org.tier,
                status=org.status,
                created_at=org.created_at,
                member_count=member_count,
                space_count=space_count,
                document_count=document_count,
                conversation_count=conversation_count,
                message_count=message_count,
                owner_email=owner_row,
                is_demo=(org.id == demo_id),
            )
        )
    return rows, total


@dataclass(frozen=True)
class PlatformBindingRow:
    id: uuid.UUID
    provider_kind: str
    display_name: str
    base_url: Optional[str]
    status: str
    models: list[str]


async def list_platform_bindings(session: AsyncSession) -> list[PlatformBindingRow]:
    """The platform-default provider bindings (shared DeepSeek, local
    embeddings, ...) — ops-only visibility; regular orgs never see these in
    their own provider list."""

    rows = (
        await session.execute(
            select(AiProviderBinding).where(
                AiProviderBinding.is_platform_default.is_(True)
            )
        )
    ).scalars().all()
    out: list[PlatformBindingRow] = []
    for r in rows:
        models = (r.extra_config or {}).get("models")
        out.append(
            PlatformBindingRow(
                id=r.id,
                provider_kind=r.provider_kind,
                display_name=r.display_name,
                base_url=r.base_url,
                status=r.status,
                models=[str(m) for m in models] if isinstance(models, list) else [],
            )
        )
    return out
