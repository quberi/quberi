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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document, DocumentAccessRule
from quberi_api.domain.enums import DOCUMENT_ACCESS_EFFECTS, DOCUMENT_ACCESS_SUBJECT_TYPES
from quberi_api.services import space_bindings
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


async def _manageable_document(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    space_id: uuid.UUID,
    document_id: uuid.UUID,
) -> Document:
    """The document, if the actor manages its space; 404 for anything they cannot see."""

    space_view = await spaces_service.get_space_for_manager(
        session, space_id=space_id, organization_id=organization_id, user_id=actor_user_id
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
