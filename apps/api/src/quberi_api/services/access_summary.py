"""A one-glance answer to "who can see this?" for a space and for a single document.

Both answers are worked out from live data on each request (nothing is stored), and the
document answer uses the same functions that decide a real read, so it cannot drift from
what people actually experience. The access-check explainer and a test hold the two to the
same verdict.

* **Space**: how many people can read the space as a whole (by role), which departments,
  positions, groups, people (or the whole organisation) it is open to, and how many of its
  documents carry exception rules.
* **Document**: how many people can read it in the end, how many of those come only from
  the space, how many only because the document is shared with them, how many the space
  would admit but a rule hides it from, and the exception counts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.documents.models import Document, DocumentAccessRule
from quberi_api.domain.enums import BLOCKED_USER_STATUSES, READABLE_SPACE_STATUSES
from quberi_api.domain.identity.models import User
from quberi_api.domain.spaces.models import Space
from quberi_api.services import doc_acl, space_bindings
from quberi_api.services import spaces as spaces_service
from quberi_api.services.authz import list_effective_space_members
from quberi_api.services.documents import access_rules
from quberi_api.services.documents import core as documents_core

# Working a document's readers out one person at a time is only done when the document has
# exception rules, and only for organisations up to this many people. Past that the exact
# figure is left out rather than making the page wait.
PER_PERSON_LIMIT = 1500


@dataclass(frozen=True)
class AccessSource:
    """Something the space is open to: a department, position, group, person or everyone."""

    subject_type: str
    subject_id: uuid.UUID
    name: str
    role_code: str


@dataclass(frozen=True)
class SpaceSummary:
    space_id: uuid.UUID
    status: str
    # People who can read the space as a whole (suspended and archived accounts excluded).
    people: int
    by_role: dict[str, int] = field(default_factory=dict)
    sources: tuple[AccessSource, ...] = ()
    documents: int = 0
    documents_with_exceptions: int = 0
    allow_rules: int = 0
    deny_rules: int = 0


@dataclass(frozen=True)
class DocumentSummary:
    document_id: uuid.UUID
    space_id: uuid.UUID
    review_state: str
    # Readers of the space as a whole.
    space_people: int
    # Who can read this document in the end. ``None`` when it is not approved yet (then only
    # its uploader and the reviewers see it) or when the organisation is too large to work out.
    readers: Optional[int]
    # Of the readers: those who can read it only because a rule shares it with them.
    shared_only: int
    # People the space admits but a deny rule hides this document from.
    hidden: int
    allow_rules: int
    deny_rules: int
    sources: tuple[AccessSource, ...] = ()


async def _space_readers(
    session: AsyncSession, *, organization_id: uuid.UUID, space: Space
) -> dict[uuid.UUID, str]:
    """``{user_id: effective_role}`` for everyone who can read the space as a whole."""

    if space.status not in READABLE_SPACE_STATUSES:
        return {}
    members = await list_effective_space_members(
        session, organization_id=organization_id, space_id=space.id
    )
    readers = {m.user_id: m.effective_role for m in members}
    if space.owner_type == "user" and space.owner_id is not None:
        readers.setdefault(space.owner_id, "owner")
    if not readers:
        return {}
    blocked = set(
        (
            await session.execute(
                select(User.id).where(
                    User.organization_id == organization_id,
                    User.id.in_(readers),
                    User.status.in_(BLOCKED_USER_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    return {uid: role for uid, role in readers.items() if uid not in blocked}


async def _sources(
    session: AsyncSession, *, organization_id: uuid.UUID, space_id: uuid.UUID
) -> tuple[AccessSource, ...]:
    views = await space_bindings.list_bindings(
        session, space_id=space_id, organization_id=organization_id
    )
    return tuple(
        AccessSource(
            subject_type=v.subject_type,
            subject_id=v.subject_id,
            name=v.subject_display_name,
            role_code=v.role_code,
        )
        for v in views
    )


async def space_summary(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    space_id: uuid.UUID,
) -> SpaceSummary:
    # Same visibility gate as the effective-members list: 404 for a space the caller cannot see.
    await spaces_service.get_space_for_manager(
        session, space_id=space_id, organization_id=organization_id, user_id=actor_user_id
    )
    space = (
        await session.execute(
            select(Space).where(Space.id == space_id, Space.organization_id == organization_id)
        )
    ).scalar_one()
    readers = await _space_readers(session, organization_id=organization_id, space=space)
    by_role: dict[str, int] = {}
    for role in readers.values():
        by_role[role] = by_role.get(role, 0) + 1

    live = (
        Document.organization_id == organization_id,
        Document.space_id == space_id,
        Document.status != "archived",
        Document.review_state == "approved",
    )
    documents = int(
        (await session.execute(select(func.count()).select_from(Document).where(*live))).scalar_one()
    )
    rule_rows = (
        await session.execute(
            select(
                DocumentAccessRule.effect,
                func.count(),
                func.count(func.distinct(DocumentAccessRule.document_id)),
            )
            .join(Document, Document.id == DocumentAccessRule.document_id)
            .where(DocumentAccessRule.organization_id == organization_id, *live)
            .group_by(DocumentAccessRule.effect)
        )
    ).all()
    counts = {effect: int(n) for effect, n, _docs in rule_rows}
    with_exceptions = int(
        (
            await session.execute(
                select(func.count(func.distinct(DocumentAccessRule.document_id)))
                .join(Document, Document.id == DocumentAccessRule.document_id)
                .where(DocumentAccessRule.organization_id == organization_id, *live)
            )
        ).scalar_one()
    )
    return SpaceSummary(
        space_id=space_id,
        status=space.status,
        people=len(readers),
        by_role=by_role,
        sources=await _sources(session, organization_id=organization_id, space_id=space_id),
        documents=documents,
        documents_with_exceptions=with_exceptions,
        allow_rules=counts.get("allow", 0),
        deny_rules=counts.get("deny", 0),
    )


async def document_summary(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    space_id: uuid.UUID,
    document_id: uuid.UUID,
) -> DocumentSummary:
    # 404 for anything the actor cannot manage, 403 when they can see but not manage.
    doc = await access_rules._manageable_document(
        session,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        space_id=space_id,
        document_id=document_id,
    )
    space = (
        await session.execute(
            select(Space).where(Space.id == space_id, Space.organization_id == organization_id)
        )
    ).scalar_one()
    space_readers = await _space_readers(session, organization_id=organization_id, space=space)

    rule_counts = {
        effect: int(n)
        for effect, n in (
            await session.execute(
                select(DocumentAccessRule.effect, func.count())
                .where(
                    DocumentAccessRule.organization_id == organization_id,
                    DocumentAccessRule.document_id == doc.id,
                )
                .group_by(DocumentAccessRule.effect)
            )
        ).all()
    }
    allow_rules = rule_counts.get("allow", 0)
    deny_rules = rule_counts.get("deny", 0)
    sources = await _sources(session, organization_id=organization_id, space_id=space_id)

    def result(readers: Optional[int], shared_only: int = 0, hidden: int = 0) -> DocumentSummary:
        return DocumentSummary(
            document_id=doc.id,
            space_id=space_id,
            review_state=doc.review_state,
            space_people=len(space_readers),
            readers=readers,
            shared_only=shared_only,
            hidden=hidden,
            allow_rules=allow_rules,
            deny_rules=deny_rules,
            sources=sources,
        )

    if doc.review_state != "approved":
        return result(None)
    if allow_rules == 0 and deny_rules == 0:
        return result(len(space_readers))

    # The document has exception rules, so ask the real gate about each person.
    people = list(
        (
            await session.execute(
                select(User.id).where(
                    User.organization_id == organization_id,
                    User.status.notin_(BLOCKED_USER_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    if len(people) > PER_PERSON_LIMIT:
        return result(None)
    readers = shared_only = hidden = 0
    for user_id in people:
        role = await documents_core._caller_space_role(
            session,
            user_id=user_id,
            organization_id=organization_id,
            space_id=space_id,
            user_blocked=False,
        )
        whole_space = documents_core._at_least(role, "viewer")
        access = await doc_acl.load_doc_access(
            session, user_id=user_id, organization_id=organization_id
        )
        denied = doc.id in access.denied
        shared = doc.id in access.granted
        if denied:
            if whole_space:
                hidden += 1
            continue
        if whole_space:
            readers += 1
        elif shared:
            readers += 1
            shared_only += 1
    return result(readers, shared_only, hidden)
