"""Managing the access rules of one document (see ``services.doc_acl`` for how they apply).

Who may change them: whoever manages the document's space, that is a space admin or
owner, the owner of a personal space, or an organisation owner/admin for an
organisation-owned space. Changing rules is about the document, not about reading it, so
a manager who is themselves denied can still open the rules to fix them.

Every change is audited with who, which document, which rule and for whom.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document, DocumentAccessRule
from quberi_api.domain.enums import DOCUMENT_ACCESS_EFFECTS, DOCUMENT_ACCESS_SUBJECT_TYPES
from quberi_api.services import access_guard, space_bindings
from quberi_api.services import spaces as spaces_service
from quberi_api.services.authz import caller_has_org_role
from quberi_api.services.documents import core
from quberi_api.services.documents.errors import DocumentPermissionError
from quberi_api.services.errors import AlreadyExistsError, InvalidInputError, NotFoundError


@dataclass(frozen=True)
class RuleView:
    rule: DocumentAccessRule
    subject_display_name: str
    subject_subtitle: Optional[str]


async def _require_space_manager(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    space_id: uuid.UUID,
) -> None:
    """Raise unless the actor manages the space (404 when it does not exist for them)."""

    space_view = await spaces_service.get_space_for_manager(
        session, space_id=space_id, organization_id=organization_id, user_id=actor_user_id
    )
    role = await core._caller_space_role(
        session, user_id=actor_user_id, organization_id=organization_id, space_id=space_id
    )
    allowed = (
        space_view.is_owner
        or core._at_least(role, "admin")
        or await caller_has_org_role(
            session,
            organization_id=organization_id,
            user_id=actor_user_id,
            allowed_codes=("owner", "admin"),
        )
    )
    if not allowed:
        raise DocumentPermissionError(
            "only a space admin or owner, or an organisation owner or admin, can change who can read a document"
        )


async def _manageable_document(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    space_id: uuid.UUID,
    document_id: uuid.UUID,
) -> Document:
    """The document, if the actor manages its space; 404 for anything they cannot see."""

    await _require_space_manager(
        session,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        space_id=space_id,
    )
    doc = (
        await session.execute(
            select(Document).where(
                Document.id == document_id,
                Document.organization_id == organization_id,
                Document.space_id == space_id,
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise NotFoundError(f"document not found: {document_id}")
    return doc


async def _view(
    session: AsyncSession, rule: DocumentAccessRule, *, organization_id: uuid.UUID
) -> RuleView:
    try:
        name, subtitle = await space_bindings.resolve_subject_display(
            session,
            subject_type=rule.subject_type,
            subject_id=rule.subject_id,
            organization_id=organization_id,
        )
    except (NotFoundError, InvalidInputError):
        # The person/group was removed after the rule was written. The rule still
        # applies to nobody, but it stays visible so it can be cleaned up.
        name, subtitle = "(removed)", None
    return RuleView(rule=rule, subject_display_name=name, subject_subtitle=subtitle)


async def list_rules(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    space_id: uuid.UUID,
    document_id: uuid.UUID,
) -> list[RuleView]:
    doc = await _manageable_document(
        session,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        space_id=space_id,
        document_id=document_id,
    )
    rules = (
        (
            await session.execute(
                select(DocumentAccessRule)
                .where(
                    DocumentAccessRule.document_id == doc.id,
                    DocumentAccessRule.organization_id == organization_id,
                )
                .order_by(DocumentAccessRule.effect, DocumentAccessRule.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [await _view(session, r, organization_id=organization_id) for r in rules]


async def add_rule(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    space_id: uuid.UUID,
    document_id: uuid.UUID,
    effect: str,
    subject_type: str,
    subject_id: uuid.UUID,
) -> RuleView:
    if effect not in DOCUMENT_ACCESS_EFFECTS:
        raise InvalidInputError(f"effect must be one of {list(DOCUMENT_ACCESS_EFFECTS)}")
    if subject_type not in DOCUMENT_ACCESS_SUBJECT_TYPES:
        raise InvalidInputError(f"subject_type must be one of {list(DOCUMENT_ACCESS_SUBJECT_TYPES)}")
    # The reads below opened a transaction; the write happens in a fresh one.
    doc = await _manageable_document(
        session,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        space_id=space_id,
        document_id=document_id,
    )
    name, subtitle = await space_bindings.resolve_subject_display(
        session,
        subject_type=subject_type,
        subject_id=subject_id,
        organization_id=organization_id,
    )
    existing = (
        await session.execute(
            select(DocumentAccessRule.id).where(
                DocumentAccessRule.document_id == doc.id,
                DocumentAccessRule.effect == effect,
                DocumentAccessRule.subject_type == subject_type,
                DocumentAccessRule.subject_id == subject_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise AlreadyExistsError("that rule already exists on this document")
    doc_id, doc_name = doc.id, doc.name
    await session.commit()
    async with session.begin():
        guard = await access_guard.watch(
            session, actor_user_id=actor_user_id, organization_id=organization_id
        )
        rule = DocumentAccessRule(
            organization_id=organization_id,
            document_id=doc_id,
            effect=effect,
            subject_type=subject_type,
            subject_id=subject_id,
            created_by_user_id=actor_user_id,
        )
        session.add(rule)
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="document.access_rule_added",
                resource_type="document",
                resource_id=doc_id,
                space_id=space_id,
                payload={
                    "document_id": str(doc_id),
                    "document_name": doc_name,
                    "effect": effect,
                    "subject_type": subject_type,
                    "subject_id": str(subject_id),
                    "display_name": name,
                },
            )
        )
        await session.flush()
        await guard.check()
        await session.refresh(rule, attribute_names=["created_at", "updated_at"])
    return RuleView(rule=rule, subject_display_name=name, subject_subtitle=subtitle)


async def remove_rule(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    space_id: uuid.UUID,
    document_id: uuid.UUID,
    rule_id: uuid.UUID,
) -> None:
    doc = await _manageable_document(
        session,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        space_id=space_id,
        document_id=document_id,
    )
    rule = (
        await session.execute(
            select(DocumentAccessRule).where(
                DocumentAccessRule.id == rule_id,
                DocumentAccessRule.document_id == doc.id,
                DocumentAccessRule.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if rule is None:
        raise NotFoundError(f"access rule not found: {rule_id}")
    doc_id, doc_name = doc.id, doc.name
    effect, subject_type, subject_id = rule.effect, rule.subject_type, rule.subject_id
    await session.commit()
    async with session.begin():
        guard = await access_guard.watch(
            session, actor_user_id=actor_user_id, organization_id=organization_id
        )
        stale = (
            await session.execute(
                select(DocumentAccessRule).where(DocumentAccessRule.id == rule_id)
            )
        ).scalar_one_or_none()
        if stale is not None:
            await session.delete(stale)
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="document.access_rule_removed",
                resource_type="document",
                resource_id=doc_id,
                space_id=space_id,
                payload={
                    "document_id": str(doc_id),
                    "document_name": doc_name,
                    "effect": effect,
                    "subject_type": subject_type,
                    "subject_id": str(subject_id),
                },
            )
        )
        await session.flush()
        await guard.check()
# ---------------------------------------------------------------------------
# Finding the document to set rules on, without being able to read the space
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManagedDocument:
    """What a manager sees of a document: enough to pick it, never its content."""

    id: uuid.UUID
    name: str
    mime_type: str
    size_bytes: int
    updated_at: object
    allow_rules: int
    deny_rules: int


@dataclass(frozen=True)
class ManagedDocumentPage:
    items: list[ManagedDocument]
    total: int
    limit: int
    offset: int


async def list_documents_for_manager(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    space_id: uuid.UUID,
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> ManagedDocumentPage:
    """The documents of a space, as names and numbers, for whoever manages its access.

    Managing who can read a document starts with finding it, and an organisation admin
    may manage an enterprise space without being able to read it. So this lists the
    documents of a space the actor manages with metadata only: no text, no download, no
    preview. It is not a way to read anything, and each use is audited.
    """

    await _require_space_manager(
        session,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        space_id=space_id,
    )
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    where = [
        Document.organization_id == organization_id,
        Document.space_id == space_id,
        Document.status != "archived",
    ]
    if q and q.strip():
        where.append(Document.name.ilike(f"%{q.strip()}%"))
    total = int(
        (await session.execute(select(func.count()).select_from(Document).where(*where))).scalar_one()
    )
    docs = (
        await session.execute(
            select(Document.id, Document.name, Document.mime_type, Document.size_bytes, Document.updated_at)
            .where(*where)
            .order_by(Document.updated_at.desc(), Document.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    counts: dict[tuple[uuid.UUID, str], int] = {}
    if docs:
        rows = await session.execute(
            select(DocumentAccessRule.document_id, DocumentAccessRule.effect, func.count())
            .where(
                DocumentAccessRule.organization_id == organization_id,
                DocumentAccessRule.document_id.in_([d[0] for d in docs]),
            )
            .group_by(DocumentAccessRule.document_id, DocumentAccessRule.effect)
        )
        counts = {(doc_id, effect): int(n) for doc_id, effect, n in rows.all()}
    items = [
        ManagedDocument(
            id=doc_id,
            name=name,
            mime_type=mime,
            size_bytes=int(size or 0),
            updated_at=updated,
            allow_rules=counts.get((doc_id, "allow"), 0),
            deny_rules=counts.get((doc_id, "deny"), 0),
        )
        for doc_id, name, mime, size, updated in docs
    ]
    await session.commit()
    async with session.begin():
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="space.documents_listed_for_access",
                resource_type="space",
                resource_id=space_id,
                space_id=space_id,
                payload={"result_count": len(items), "searched": bool(q and q.strip())},
            )
        )
    return ManagedDocumentPage(items=items, total=total, limit=limit, offset=offset)
