"""Upload approval: a space can ask that what editors upload is approved before it is indexed.

The owner of a space turns the setting on. From then on a file uploaded by someone who is not
an owner or admin of that space waits as **pending**:

* nothing is indexed, so it cannot be found by search or quoted in a chat answer;
* only the uploader and the space's reviewers (its owners and admins) can see it, and
  reviewers can open and download it so they can judge it;
* a reviewer **approves** it, which queues it for indexing, or **rejects** it with a reason.
  A rejected file stays on record (the uploader sees the reason) until it is approved after
  all, or deleted.

Owners and admins upload straight in: they are the reviewers. Turning the setting off does not
release what is waiting; a reviewer still decides. Every step is audited.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document
from quberi_api.domain.identity.models import User
from quberi_api.domain.spaces.models import Space
from quberi_api.services.errors import ForbiddenError, InvalidInputError, NotFoundError

SETTING_KEY = "upload_review"
MAX_NOTE_CHARS = 500
MAX_BATCH = 100
_REVIEWER_ROLES = ("owner", "admin")


def space_requires_review(attributes: Optional[dict]) -> bool:
    return bool((attributes or {}).get(SETTING_KEY))


@dataclass(frozen=True)
class ReviewScope:
    """Which documents of a space this person may see, as far as approval is concerned."""

    user_id: uuid.UUID
    reviewer: bool

    def clause(self):
        """SQL on ``documents``: approved ones, the person's own, and everything for reviewers."""

        if self.reviewer:
            return true()
        return or_(Document.review_state == "approved", Document.uploaded_by_user_id == self.user_id)

    def allows(self, doc: Document) -> bool:
        return (
            self.reviewer
            or doc.review_state == "approved"
            or doc.uploaded_by_user_id == self.user_id
        )


async def _role(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID, space_id: uuid.UUID
) -> Optional[str]:
    from quberi_api.services.documents import core

    return await core._caller_space_role(
        session, user_id=user_id, organization_id=organization_id, space_id=space_id
    )


def review_scope_for_role(user_id: uuid.UUID, role: Optional[str]) -> ReviewScope:
    return ReviewScope(user_id=user_id, reviewer=role in _REVIEWER_ROLES)


async def initial_state(
    session: AsyncSession,
    *,
    space_id: uuid.UUID,
    organization_id: uuid.UUID,
    role: Optional[str],
) -> str:
    """``pending`` for an upload that needs approval, else ``approved``."""

    attributes = (
        await session.execute(
            select(Space.attributes).where(
                Space.id == space_id, Space.organization_id == organization_id
            )
        )
    ).scalar_one_or_none()
    if space_requires_review(attributes) and role not in _REVIEWER_ROLES:
        return "pending"
    return "approved"


async def set_requirement(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    space_id: uuid.UUID,
    enabled: bool,
) -> bool:
    """Turn upload approval on or off. Only an owner of the space may."""

    role = await _role(
        session, organization_id=organization_id, user_id=actor_user_id, space_id=space_id
    )
    if role is None:
        raise NotFoundError(f"space not found: {space_id}")
    if role != "owner":
        raise ForbiddenError("only an owner of the space can change whether uploads need approval")
    space = (
        await session.execute(
            select(Space).where(Space.id == space_id, Space.organization_id == organization_id)
        )
    ).scalar_one()
    if space.preset == "personal":
        raise InvalidInputError("a personal space has no one to approve uploads")
    await session.commit()
    async with session.begin():
        space = (await session.execute(select(Space).where(Space.id == space_id))).scalar_one()
        attributes = dict(space.attributes or {})
        if bool(attributes.get(SETTING_KEY)) != enabled:
            if enabled:
                attributes[SETTING_KEY] = True
            else:
                attributes.pop(SETTING_KEY, None)
            space.attributes = attributes
            session.add(
                AuditEvent(
                    organization_id=organization_id,
                    actor_type="user",
                    actor_id=actor_user_id,
                    action="space.upload_review_changed",
                    resource_type="space",
                    resource_id=space_id,
                    space_id=space_id,
                    payload={"enabled": enabled},
                )
            )
    return enabled


@dataclass(frozen=True)
class ReviewItem:
    id: uuid.UUID
    name: str
    mime_type: str
    size_bytes: int
    uploader_name: str
    created_at: datetime
    review_state: str
    review_note: Optional[str]
    reviewed_by_name: Optional[str]


async def _require_reviewer(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID, space_id: uuid.UUID
) -> None:
    role = await _role(
        session, organization_id=organization_id, user_id=user_id, space_id=space_id
    )
    if role is None:
        raise NotFoundError(f"space not found: {space_id}")
    if role not in _REVIEWER_ROLES:
        raise ForbiddenError("only an owner or admin of the space can review uploads")


async def list_for_review(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    space_id: uuid.UUID,
    state: str = "pending",
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[ReviewItem], int]:
    if state not in ("pending", "rejected"):
        raise InvalidInputError("state must be 'pending' or 'rejected'")
    await _require_reviewer(
        session, organization_id=organization_id, user_id=actor_user_id, space_id=space_id
    )
    where = [
        Document.organization_id == organization_id,
        Document.space_id == space_id,
        Document.status != "archived",
        Document.review_state == state,
    ]
    total = int(
        (await session.execute(select(func.count()).select_from(Document).where(*where))).scalar_one()
    )
    uploader = User.__table__.alias("uploader")
    reviewer = User.__table__.alias("reviewer")
    rows = (
        await session.execute(
            select(Document, uploader.c.display_name, reviewer.c.display_name)
            .outerjoin(uploader, uploader.c.id == Document.uploaded_by_user_id)
            .outerjoin(reviewer, reviewer.c.id == Document.reviewed_by_user_id)
            .where(*where)
            .order_by(Document.created_at.asc())
            .limit(max(1, min(limit, 200)))
            .offset(max(0, offset))
        )
    ).all()
    items = [
        ReviewItem(
            id=d.id,
            name=d.name,
            mime_type=d.mime_type,
            size_bytes=int(d.size_bytes or 0),
            uploader_name=up or "",
            created_at=d.created_at,
            review_state=d.review_state,
            review_note=d.review_note,
            reviewed_by_name=rv,
        )
        for d, up, rv in rows
    ]
    return items, total


async def decide(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    space_id: uuid.UUID,
    document_ids: list[uuid.UUID],
    approve: bool,
    note: str = "",
) -> list[uuid.UUID]:
    """Approve or reject documents. Returns the ids that were changed.

    Approving queues indexing. Rejecting needs a reason. A rejected file may still be
    approved later; an approved one is final here (delete it to remove it).
    """

    note = (note or "").strip()
    if len(note) > MAX_NOTE_CHARS:
        raise InvalidInputError(f"the note is longer than {MAX_NOTE_CHARS} characters")
    if not approve and not note:
        raise InvalidInputError("say why the upload is rejected")
    ids = list(dict.fromkeys(document_ids))
    if not ids:
        raise InvalidInputError("choose at least one document")
    if len(ids) > MAX_BATCH:
        raise InvalidInputError(f"at most {MAX_BATCH} documents at a time")
    await _require_reviewer(
        session, organization_id=organization_id, user_id=actor_user_id, space_id=space_id
    )
    await session.commit()
    changed: list[uuid.UUID] = []
    async with session.begin():
        docs = (
            (
                await session.execute(
                    select(Document).where(
                        Document.id.in_(ids),
                        Document.organization_id == organization_id,
                        Document.space_id == space_id,
                        Document.status != "archived",
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(docs) != len(ids):
            raise NotFoundError("one of the documents was not found in this space")
        allowed_from = ("pending", "rejected") if approve else ("pending",)
        for doc in docs:
            if doc.review_state not in allowed_from:
                raise InvalidInputError(f"{doc.name!r} is already {doc.review_state}")
        now = datetime.now(timezone.utc)
        for doc in docs:
            doc.review_state = "approved" if approve else "rejected"
            doc.reviewed_by_user_id = actor_user_id
            doc.reviewed_at = now
            doc.review_note = note or None
            changed.append(doc.id)
            session.add(
                AuditEvent(
                    organization_id=organization_id,
                    actor_type="user",
                    actor_id=actor_user_id,
                    action="document.review_approved" if approve else "document.review_rejected",
                    resource_type="document",
                    resource_id=doc.id,
                    space_id=space_id,
                    payload={"name": doc.name, "note": note},
                )
            )
    if approve:
        from quberi_api.workers.enqueue import enqueue_index_document

        for doc_id in changed:
            await enqueue_index_document(doc_id)
    return changed


@dataclass(frozen=True)
class PendingSpace:
    space_id: uuid.UUID
    space_name: str
    pending: int


async def pending_inbox(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID
) -> list[PendingSpace]:
    """Spaces this person reviews that have uploads waiting, with the count."""

    rows = (
        await session.execute(
            select(Space.id, Space.name, func.count(Document.id))
            .join(Document, Document.space_id == Space.id)
            .where(
                Space.organization_id == organization_id,
                Space.status == "active",
                Document.review_state == "pending",
                Document.status != "archived",
            )
            .group_by(Space.id, Space.name)
            .order_by(Space.name)
        )
    ).all()
    result: list[PendingSpace] = []
    for space_id, name, count in rows:
        role = await _role(
            session, organization_id=organization_id, user_id=user_id, space_id=space_id
        )
        if role in _REVIEWER_ROLES:
            result.append(PendingSpace(space_id=space_id, space_name=name, pending=int(count)))
    return result


