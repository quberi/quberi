"""The admin console's Overview: the state of the organisation at a glance.

Everything here is counted live from the tables on each request (nothing is stored). It is
numbers and names only, never document text, and it is for organisation owners and admins.

Privacy rule for the one list that names documents ("most cited"): a document appears only if
it lives in an organisation-owned (enterprise) space, which admins already manage by name, or
the admin could read it themselves. Documents in someone's personal or private team space are
never named here, however often they are cited.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.authz.models import RoleBinding
from quberi_api.domain.conversations.models import Message
from quberi_api.domain.documents.models import Document
from quberi_api.domain.identity.models import User
from quberi_api.domain.oauth.models import OAuthGrant
from quberi_api.domain.spaces.models import Space, SpaceAccessRequest
from quberi_api.services import doc_acl, offboarding
from quberi_api.services.authz import list_accessible_space_ids

TREND_DAYS = 14
CITED_DAYS = 30
CITED_SHOWN = 10
# How many of the most cited candidates are looked at before the visibility rule is applied.
CITED_CANDIDATES = 80
NAMES_SHOWN = 5


@dataclass(frozen=True)
class MemberCounts:
    total: int = 0
    active: int = 0
    invited: int = 0
    suspended: int = 0


@dataclass(frozen=True)
class SpaceCounts:
    # Active spaces by kind; archived and frozen ones are counted apart.
    total: int = 0
    enterprise: int = 0
    team: int = 0
    personal: int = 0
    archived: int = 0
    frozen: int = 0


@dataclass(frozen=True)
class DocumentCounts:
    total: int = 0
    indexed: int = 0
    failed: int = 0
    waiting: int = 0


@dataclass(frozen=True)
class ExternalCounts:
    connected_tools: int = 0
    calls_7d: int = 0


@dataclass(frozen=True)
class AttentionItem:
    """Something an admin should look at. Only present when ``count`` is above zero."""

    code: str
    count: int
    names: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrendPoint:
    day: str  # YYYY-MM-DD in the viewer's time zone
    documents: int
    questions: int


@dataclass(frozen=True)
class CitedDocument:
    document_id: uuid.UUID
    name: str
    space_id: uuid.UUID
    space_name: str
    citations: int
    last_cited_at: datetime


@dataclass(frozen=True)
class Overview:
    members: MemberCounts
    spaces: SpaceCounts
    documents: DocumentCounts
    external: ExternalCounts
    attention: list[AttentionItem] = field(default_factory=list)
    trend: list[TrendPoint] = field(default_factory=list)
    top_cited: list[CitedDocument] = field(default_factory=list)
    cited_days: int = CITED_DAYS


async def _members(session: AsyncSession, organization_id: uuid.UUID) -> MemberCounts:
    rows = (
        await session.execute(
            select(User.status, func.count())
            .where(User.organization_id == organization_id)
            .group_by(User.status)
        )
    ).all()
    by = {status: int(n) for status, n in rows}
    return MemberCounts(
        total=sum(by.values()),
        active=by.get("active", 0),
        invited=by.get("invited", 0),
        suspended=by.get("suspended", 0) + by.get("archived", 0),
    )


async def _spaces(session: AsyncSession, organization_id: uuid.UUID) -> SpaceCounts:
    rows = (
        await session.execute(
            select(Space.preset, Space.owner_type, Space.status, func.count())
            .where(Space.organization_id == organization_id)
            .group_by(Space.preset, Space.owner_type, Space.status)
        )
    ).all()
    enterprise = team = personal = archived = frozen = 0
    for preset, owner_type, status, n in rows:
        n = int(n)
        if status == "archived":
            archived += n
        elif status == "frozen":
            frozen += n
        elif preset == "personal":
            personal += n
        elif owner_type == "organization":
            enterprise += n
        else:
            team += n
    return SpaceCounts(
        total=enterprise + team + personal,
        enterprise=enterprise,
        team=team,
        personal=personal,
        archived=archived,
        frozen=frozen,
    )


async def _documents(session: AsyncSession, organization_id: uuid.UUID) -> DocumentCounts:
    rows = (
        await session.execute(
            select(Document.indexing_state, func.count())
            .where(Document.organization_id == organization_id, Document.status != "archived")
            .group_by(Document.indexing_state)
        )
    ).all()
    by = {state: int(n) for state, n in rows}
    return DocumentCounts(
        total=sum(by.values()),
        indexed=by.get("indexed", 0),
        failed=by.get("failed", 0),
        waiting=by.get("not_indexed", 0) + by.get("indexing", 0),
    )


async def _external(session: AsyncSession, organization_id: uuid.UUID, now: datetime) -> ExternalCounts:
    tools = (
        await session.execute(
            select(func.count())
            .select_from(OAuthGrant)
            .where(
                OAuthGrant.organization_id == organization_id,
                OAuthGrant.revoked_at.is_(None),
                OAuthGrant.refresh_expires_at > now,
                OAuthGrant.absolute_expires_at > now,
            )
        )
    ).scalar_one()
    calls = (
        await session.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.organization_id == organization_id,
                AuditEvent.created_at >= now - timedelta(days=7),
                AuditEvent.action.like("knowledge.external\\_%", escape="\\"),
                # A refused duplicate upload is not a call that did something.
                AuditEvent.action.notlike("%\\_duplicate", escape="\\"),
            )
        )
    ).scalar_one()
    return ExternalCounts(connected_tools=int(tools), calls_7d=int(calls))


async def _attention(session: AsyncSession, organization_id: uuid.UUID) -> list[AttentionItem]:
    items: list[AttentionItem] = []

    pending = (
        await session.execute(
            select(func.count())
            .select_from(Document)
            .where(
                Document.organization_id == organization_id,
                Document.review_state == "pending",
                Document.status != "archived",
            )
        )
    ).scalar_one()
    if pending:
        items.append(AttentionItem("pending_reviews", int(pending)))

    requests = (
        await session.execute(
            select(func.count())
            .select_from(SpaceAccessRequest)
            .where(
                SpaceAccessRequest.organization_id == organization_id,
                SpaceAccessRequest.status == "pending",
            )
        )
    ).scalar_one()
    if requests:
        items.append(AttentionItem("access_requests", int(requests)))

    failed = (
        await session.execute(
            select(func.count())
            .select_from(Document)
            .where(
                Document.organization_id == organization_id,
                Document.indexing_state == "failed",
                Document.status != "archived",
            )
        )
    ).scalar_one()
    if failed:
        items.append(AttentionItem("index_failed", int(failed)))

    # Deactivated people who still own a shared space nobody has taken over.
    suspended = (
        await session.execute(
            select(User.id, User.display_name)
            .where(User.organization_id == organization_id, User.status == "suspended")
            .order_by(User.display_name)
        )
    ).all()
    stranded: list[str] = []
    for user_id, display_name in suspended:
        owned = await offboarding._owned_space_rows(
            session, organization_id=organization_id, member_id=user_id
        )
        if owned:
            stranded.append(display_name)
    if stranded:
        items.append(AttentionItem("suspended_owning_spaces", len(stranded), tuple(stranded[:NAMES_SHOWN])))

    open_rows = (
        await session.execute(
            select(Space.name, Space.owner_type)
            .join(RoleBinding, RoleBinding.resource_id == Space.id)
            .where(
                Space.organization_id == organization_id,
                Space.status == "active",
                Space.preset != "personal",
                RoleBinding.organization_id == organization_id,
                RoleBinding.resource_type == "space",
                RoleBinding.subject_type == "organization",
                RoleBinding.subject_id == organization_id,
            )
            .distinct()
            .order_by(Space.name)
        )
    ).all()
    if open_rows:
        # Only enterprise spaces are named: admins manage those by name already.
        names = tuple(name for name, owner_type in open_rows if owner_type == "organization")[:NAMES_SHOWN]
        items.append(AttentionItem("org_wide_spaces", len(open_rows), names))
    return items


async def _trend(
    session: AsyncSession, organization_id: uuid.UUID, now: datetime, tz_offset_minutes: int
) -> list[TrendPoint]:
    shift = timedelta(minutes=tz_offset_minutes)
    today = (now + shift).date()
    first = today - timedelta(days=TREND_DAYS - 1)
    # Midnight of the first day in the viewer's zone, back in UTC.
    since = datetime(first.year, first.month, first.day, tzinfo=timezone.utc) - shift

    doc_day = func.date_trunc("day", Document.created_at + shift).label("day")
    docs = dict(
        (
            await session.execute(
                select(doc_day, func.count())
                .where(Document.organization_id == organization_id, Document.created_at >= since)
                .group_by(doc_day)
            )
        ).all()
    )
    msg_day = func.date_trunc("day", Message.created_at + shift).label("day")
    asked = dict(
        (
            await session.execute(
                select(msg_day, func.count())
                .where(
                    Message.organization_id == organization_id,
                    Message.role == "user",
                    Message.created_at >= since,
                )
                .group_by(msg_day)
            )
        ).all()
    )

    def lookup(counts: dict, day) -> int:
        for key, n in counts.items():
            if key.date() == day:
                return int(n)
        return 0

    out = []
    for i in range(TREND_DAYS):
        day = first + timedelta(days=i)
        out.append(TrendPoint(day=day.isoformat(), documents=lookup(docs, day), questions=lookup(asked, day)))
    return out


_CITED_SQL = text(
    """
    SELECT c->>'document_id' AS document_id,
           count(DISTINCT m.id) AS n,
           max(m.created_at) AS last_cited_at
      FROM messages m
     CROSS JOIN LATERAL jsonb_array_elements(
           CASE WHEN jsonb_typeof(m.content->'citations') = 'array'
                THEN m.content->'citations' ELSE '[]'::jsonb END) AS c
     WHERE m.organization_id = :org
       AND m.role = 'assistant'
       AND m.created_at >= :since
       AND coalesce(c->>'used', 'true') = 'true'
       AND c->>'document_id' IS NOT NULL
     GROUP BY 1
     ORDER BY n DESC, last_cited_at DESC
     LIMIT :limit
    """
)


async def _top_cited(
    session: AsyncSession, organization_id: uuid.UUID, actor_user_id: uuid.UUID, now: datetime
) -> list[CitedDocument]:
    rows = (
        await session.execute(
            _CITED_SQL,
            {
                "org": organization_id,
                "since": now - timedelta(days=CITED_DAYS),
                "limit": CITED_CANDIDATES,
            },
        )
    ).all()
    if not rows:
        return []
    ids: list[uuid.UUID] = []
    for document_id, _n, _last in rows:
        try:
            ids.append(uuid.UUID(str(document_id)))
        except ValueError:
            continue
    docs = {
        d.id: (d.name, d.space_id, space_name, owner_type)
        for d, space_name, owner_type in (
            await session.execute(
                select(Document, Space.name, Space.owner_type)
                .join(Space, Space.id == Document.space_id)
                .where(
                    Document.organization_id == organization_id,
                    Document.id.in_(ids),
                    Document.status != "archived",
                )
            )
        ).all()
    }
    readable = await list_accessible_space_ids(
        session, user_id=actor_user_id, organization_id=organization_id
    )
    access = await doc_acl.load_doc_access(
        session, user_id=actor_user_id, organization_id=organization_id
    )
    out: list[CitedDocument] = []
    for document_id, n, last in rows:
        try:
            doc_id = uuid.UUID(str(document_id))
        except ValueError:
            continue
        found = docs.get(doc_id)
        if found is None:
            continue
        name, space_id, space_name, owner_type = found
        if owner_type != "organization" and not access.allows(
            document_id=doc_id, space_id=space_id, readable_spaces=readable
        ):
            continue
        out.append(
            CitedDocument(
                document_id=doc_id,
                name=name,
                space_id=space_id,
                space_name=space_name,
                citations=int(n),
                last_cited_at=last,
            )
        )
        if len(out) >= CITED_SHOWN:
            break
    return out


async def build_overview(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    tz_offset_minutes: int = 0,
) -> Overview:
    now = datetime.now(timezone.utc)
    tz_offset_minutes = max(-14 * 60, min(14 * 60, int(tz_offset_minutes)))
    return Overview(
        members=await _members(session, organization_id),
        spaces=await _spaces(session, organization_id),
        documents=await _documents(session, organization_id),
        external=await _external(session, organization_id, now),
        attention=await _attention(session, organization_id),
        trend=await _trend(session, organization_id, now, tz_offset_minutes),
        top_cited=await _top_cited(session, organization_id, actor_user_id, now),
    )

