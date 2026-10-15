"""Read-side audit event helpers.

Everything else in ``services/`` writes ``AuditEvent`` rows; this module
provides the corresponding read path. It exists mainly to feed the "live
org pulse" surface in the sidebar and (eventually) a real audit log page.

Design
------
* One SQL round-trip pulls the newest ``limit`` rows for the org.
* A second pass batch-loads display names for every ``actor_id`` and
  ``resource_id`` we saw, grouped by type. This avoids N+1 chatter even
  for large limits.
* Deleted actors / resources fall back to a ``(deleted)`` sentinel so the
  UI can render something rather than showing a bare uuid.
* Space name is fetched separately when ``space_id`` is set, because a
  binding event's ``resource_id`` is the binding itself, not the space.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document
from quberi_api.domain.identity.models import ServiceAccount, User
from quberi_api.domain.org.models import Group, OrganizationUnit
from quberi_api.domain.positions.models import Position
from quberi_api.domain.spaces.models import Space


@dataclass(frozen=True)
class RecentEventView:
    """One audit event enriched with human-readable identity fields."""

    id: uuid.UUID
    created_at: object  # datetime, but avoid the extra import at read sites
    actor_type: str
    actor_id: Optional[uuid.UUID]
    actor_display_name: str
    action: str
    resource_type: str
    resource_id: Optional[uuid.UUID]
    resource_display_name: str
    space_id: Optional[uuid.UUID]
    space_display_name: Optional[str]
    payload: dict
    # Names of the documents retrieval put in front of the model for a
    # ``message.sent`` event (``name`` is None once the document was deleted).
    retrieved_documents: list = field(default_factory=list)


# Resource types whose display name we know how to resolve. Anything not
# listed here is passed through as its raw resource_type string in
# ``resource_display_name`` — the UI's i18n template can decide what to do.
_RESOURCE_TABLES: dict[str, tuple[type, str]] = {
    "user": (User, "display_name"),
    "position": (Position, "name"),
    "group": (Group, "name"),
    "organization_unit": (OrganizationUnit, "name"),
    "space": (Space, "name"),
    "service_account": (ServiceAccount, "name"),
}


async def list_recent_events(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    limit: int = 20,
) -> list[RecentEventView]:
    """Return the newest ``limit`` audit events, enriched for the UI.

    ``limit`` is clamped to ``[1, 100]`` to keep the sidebar's poll cost
    small and predictable.
    """

    clamped = max(1, min(limit, 100))

    stmt = (
        select(AuditEvent)
        .where(AuditEvent.organization_id == organization_id)
        .order_by(AuditEvent.created_at.desc())
        .limit(clamped)
    )
    rows: list[AuditEvent] = list(
        (await session.execute(stmt)).scalars().all()
    )
    return await enrich_events(session, rows, organization_id=organization_id)


async def enrich_events(
    session: AsyncSession,
    rows: list[AuditEvent],
    *,
    organization_id: uuid.UUID,
) -> list[RecentEventView]:
    """Attach display names (actor, resource, space, retrieved documents)."""

    if not rows:
        return []

    # Bucket ids per (type-table). Actor identifiers reference either
    # ``users`` or ``service_accounts`` depending on ``actor_type``;
    # resources vary widely.
    user_ids: set[uuid.UUID] = set()
    service_account_ids: set[uuid.UUID] = set()
    resource_ids_by_type: dict[str, set[uuid.UUID]] = {}
    space_ids: set[uuid.UUID] = set()

    for r in rows:
        if r.actor_id is not None:
            if r.actor_type == "user":
                user_ids.add(r.actor_id)
            elif r.actor_type == "service_account":
                service_account_ids.add(r.actor_id)
            # "system" actor has no id to resolve.
        if r.resource_id is not None:
            if r.resource_type == "user":
                user_ids.add(r.resource_id)
            elif r.resource_type == "service_account":
                service_account_ids.add(r.resource_id)
            else:
                resource_ids_by_type.setdefault(r.resource_type, set()).add(
                    r.resource_id
                )
        if r.space_id is not None:
            space_ids.add(r.space_id)

    # Batch-load display names for each bucket.
    user_names: dict[uuid.UUID, str] = {}
    if user_ids:
        for uid, name in (
            await session.execute(
                select(User.id, User.display_name).where(User.id.in_(user_ids))
            )
        ).all():
            user_names[uid] = name

    sa_names: dict[uuid.UUID, str] = {}
    if service_account_ids:
        for sid, name in (
            await session.execute(
                select(ServiceAccount.id, ServiceAccount.name).where(
                    ServiceAccount.id.in_(service_account_ids)
                )
            )
        ).all():
            sa_names[sid] = name

    resource_names: dict[tuple[str, uuid.UUID], str] = {}
    for rtype, ids in resource_ids_by_type.items():
        table = _RESOURCE_TABLES.get(rtype)
        if table is None or not ids:
            continue
        model, name_attr = table
        col = getattr(model, name_attr)
        stmt = select(model.id, col).where(model.id.in_(ids))
        # Some resources (Position, Group, OrganizationUnit, Space) are
        # tenant-scoped and always match on organization_id, but we don't
        # filter here on purpose — an audit row must survive a leaky ORM.
        # The IN clause + ids we harvested already scope us safely.
        for rid, name in (await session.execute(stmt)).all():
            resource_names[(rtype, rid)] = name

    space_names: dict[uuid.UUID, str] = {}
    if space_ids:
        for sid, name in (
            await session.execute(
                select(Space.id, Space.name).where(Space.id.in_(space_ids))
            )
        ).all():
            space_names[sid] = name

    # Documents retrieval handed to the model, for message events. Scoped to
    # this organisation so an id from elsewhere can never reveal a name.
    retrieved_ids: set[uuid.UUID] = set()
    for r in rows:
        for raw in (r.payload or {}).get("retrieved_document_ids") or []:
            try:
                retrieved_ids.add(uuid.UUID(str(raw)))
            except ValueError:
                continue
    document_names: dict[uuid.UUID, str] = {}
    if retrieved_ids:
        for did, name in (
            await session.execute(
                select(Document.id, Document.name).where(
                    Document.id.in_(retrieved_ids),
                    Document.organization_id == organization_id,
                )
            )
        ).all():
            document_names[did] = name

    # Materialise the enriched view.
    result: list[RecentEventView] = []
    for r in rows:
        actor_name = ""
        if r.actor_type == "user" and r.actor_id is not None:
            actor_name = user_names.get(r.actor_id, "")
        elif r.actor_type == "service_account" and r.actor_id is not None:
            actor_name = sa_names.get(r.actor_id, "")
        elif r.actor_type == "system":
            actor_name = "System"

        resource_name = ""
        if r.resource_id is not None:
            if r.resource_type == "user":
                resource_name = user_names.get(r.resource_id, "")
            elif r.resource_type == "service_account":
                resource_name = sa_names.get(r.resource_id, "")
            else:
                resource_name = resource_names.get(
                    (r.resource_type, r.resource_id), ""
                )

        space_name = (
            space_names.get(r.space_id, "") if r.space_id is not None else None
        )

        retrieved_documents: list = []
        for raw in (r.payload or {}).get("retrieved_document_ids") or []:
            try:
                did = uuid.UUID(str(raw))
            except ValueError:
                continue
            retrieved_documents.append(
                {"id": str(did), "name": document_names.get(did)}
            )
        result.append(
            RecentEventView(
                id=r.id,
                created_at=r.created_at,
                actor_type=r.actor_type,
                actor_id=r.actor_id,
                actor_display_name=actor_name,
                action=r.action,
                resource_type=r.resource_type,
                resource_id=r.resource_id,
                resource_display_name=resource_name,
                space_id=r.space_id,
                space_display_name=space_name,
                payload=r.payload or {},
                retrieved_documents=retrieved_documents,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Searchable log (admin audit page)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventPage:
    items: list[RecentEventView]
    next_cursor: Optional[str]


def encode_cursor(created_at: datetime, event_id: uuid.UUID) -> str:
    raw = json.dumps({"t": created_at.isoformat(), "i": str(event_id)})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    from quberi_api.services.errors import InvalidInputError

    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return datetime.fromisoformat(data["t"]), uuid.UUID(data["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise InvalidInputError("invalid cursor") from exc


def _filters(
    *,
    organization_id: uuid.UUID,
    action: Optional[str],
    actor_id: Optional[uuid.UUID],
    space_id: Optional[uuid.UUID],
    since: Optional[datetime],
    until: Optional[datetime],
) -> list:
    conditions = [AuditEvent.organization_id == organization_id]
    if action:
        conditions.append(AuditEvent.action == action)
    if actor_id is not None:
        conditions.append(AuditEvent.actor_id == actor_id)
    if space_id is not None:
        conditions.append(AuditEvent.space_id == space_id)
    if since is not None:
        conditions.append(AuditEvent.created_at >= since)
    if until is not None:
        conditions.append(AuditEvent.created_at < until)
    return conditions


async def search_events(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    limit: int = 50,
    cursor: Optional[str] = None,
    action: Optional[str] = None,
    actor_id: Optional[uuid.UUID] = None,
    space_id: Optional[uuid.UUID] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> EventPage:
    """Newest-first page of the organisation's audit log.

    Pages are keyed on ``(created_at, id)`` rather than an offset, so new
    events arriving while someone scrolls never shift or repeat rows.
    """

    limit = max(1, min(limit, 100))
    stmt = select(AuditEvent).where(
        *_filters(
            organization_id=organization_id,
            action=action,
            actor_id=actor_id,
            space_id=space_id,
            since=since,
            until=until,
        )
    )
    if cursor:
        c_at, c_id = decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(AuditEvent.created_at, AuditEvent.id) < tuple_(c_at, c_id)
        )
    stmt = stmt.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(
        limit + 1
    )
    rows = list((await session.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = await enrich_events(session, rows, organization_id=organization_id)
    next_cursor = (
        encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    )
    return EventPage(items=items, next_cursor=next_cursor)


async def list_actions(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> list[str]:
    """Distinct action codes this organisation has produced (filter choices)."""

    rows = await session.execute(
        select(AuditEvent.action)
        .where(AuditEvent.organization_id == organization_id)
        .distinct()
        .order_by(AuditEvent.action)
        .limit(200)
    )
    return [a for (a,) in rows.all()]
