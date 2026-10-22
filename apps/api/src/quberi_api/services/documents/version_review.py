"""Deciding on a new version that is waiting for approval.

In a space that approves uploads, an editor's new version of a document waits (see
:func:`new_version.upload_new_version`). An owner or admin of the space decides, as for a new
document:

* **approve**: the version becomes the document's current file and is indexed; the document
  starts to show it, and the versions before it stay in the history, searchable under their own
  numbers. A version that was refused may be approved later, unless a newer one is already the
  current file;
* **reject** (needs a reason): the version stays on record, the document keeps its file, and the
  uploader sees the reason.

Every step is audited. All the versions in one request are decided together or not at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document, DocumentVersion
from quberi_api.domain.identity.models import User
from quberi_api.services.documents import new_version
from quberi_api.services.documents import review as review_service
from quberi_api.services.documents import versions as versions_service
from quberi_api.services.documents.duplicates import DUPLICATE_INDEX, find_live_duplicate
from quberi_api.services.errors import InvalidInputError, NotFoundError

MAX_BATCH = review_service.MAX_BATCH
MAX_NOTE_CHARS = review_service.MAX_NOTE_CHARS


@dataclass(frozen=True)
class VersionReviewItem:
    document_id: uuid.UUID
    document_name: str
    version_number: int
    filename: str
    mime_type: str
    size_bytes: int
    uploader_name: str
    created_at: datetime
    note: Optional[str]
    review_state: str
    review_note: Optional[str]
    reviewed_by_name: Optional[str]


async def list_for_review(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    space_id: uuid.UUID,
    state: str = "pending",
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[VersionReviewItem], int]:
    """New versions waiting (or refused) in a space, for its reviewers, oldest first."""

    if state not in ("pending", "rejected"):
        raise InvalidInputError("state must be 'pending' or 'rejected'")
    await review_service._require_reviewer(
        session, organization_id=organization_id, user_id=actor_user_id, space_id=space_id
    )
    where = [
        Document.organization_id == organization_id,
        Document.space_id == space_id,
        Document.status != "archived",
        DocumentVersion.review_state == state,
    ]
    total = int(
        (
            await session.execute(
                select(func.count())
                .select_from(DocumentVersion)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(*where)
            )
        ).scalar_one()
    )
    uploader = User.__table__.alias("uploader")
    reviewer = User.__table__.alias("reviewer")
    rows = (
        await session.execute(
            select(DocumentVersion, Document.name, uploader.c.display_name, reviewer.c.display_name)
            .join(Document, Document.id == DocumentVersion.document_id)
            .outerjoin(uploader, uploader.c.id == DocumentVersion.created_by_user_id)
            .outerjoin(reviewer, reviewer.c.id == DocumentVersion.reviewed_by_user_id)
            .where(*where)
            .order_by(DocumentVersion.created_at.asc())
            .limit(max(1, min(limit, 200)))
            .offset(max(0, offset))
        )
    ).all()
    items = [
        VersionReviewItem(
            document_id=v.document_id,
            document_name=doc_name,
            version_number=v.version_number,
            filename=v.filename,
            mime_type=v.mime_type,
            size_bytes=int(v.size_bytes or 0),
            uploader_name=up or "",
            created_at=v.created_at,
            note=v.note,
            review_state=v.review_state,
            review_note=v.review_note,
            reviewed_by_name=rv,
        )
        for v, doc_name, up, rv in rows
    ]
    return items, total


async def decide(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    space_id: uuid.UUID,
    targets: list[tuple[uuid.UUID, int]],
    approve: bool,
    note: str = "",
) -> list[tuple[uuid.UUID, int]]:
    """Approve or reject versions, each named by ``(document id, version number)``.

    Returns the ones that were changed. Approving makes the version the document's current
    file and queues indexing. Rejecting needs a reason.
    """

    note = (note or "").strip()
    if len(note) > MAX_NOTE_CHARS:
        raise InvalidInputError(f"the note is longer than {MAX_NOTE_CHARS} characters")
    if not approve and not note:
        raise InvalidInputError("say why the version is rejected")
    wanted = list(dict.fromkeys(targets))
    if not wanted:
        raise InvalidInputError("choose at least one version")
    if len(wanted) > MAX_BATCH:
        raise InvalidInputError(f"at most {MAX_BATCH} versions at a time")
    await review_service._require_reviewer(
        session, organization_id=organization_id, user_id=actor_user_id, space_id=space_id
    )
    await session.commit()
    changed: list[tuple[uuid.UUID, int]] = []
    became_current: list[tuple[uuid.UUID, str, str]] = []
    try:
        async with session.begin():
            now = datetime.now(timezone.utc)
            allowed_from = ("pending", "rejected") if approve else ("pending",)
            # Lock the documents first, in a fixed order, so two reviewers cannot interleave.
            doc_ids = sorted({d for d, _ in wanted}, key=str)
            docs = {
                d.id: d
                for d in (
                    await session.execute(
                        select(Document)
                        .where(
                            Document.id.in_(doc_ids),
                            Document.organization_id == organization_id,
                            Document.space_id == space_id,
                        )
                        .order_by(Document.id)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            }
            if len(docs) != len(doc_ids):
                raise NotFoundError("one of the documents was not found in this space")
            for doc_id, number in wanted:
                doc = docs[doc_id]
                version = await versions_service.get_version(session, doc_id, number)
                if version is None:
                    raise NotFoundError(f"version not found: {number}")
                if version.review_state not in allowed_from:
                    raise InvalidInputError(
                        f"{doc.name!r} version {number} is already {version.review_state}"
                    )
                if approve:
                    previous_mime = doc.mime_type
                    await _check_can_become_current(session, doc, version)
                    other = await find_live_duplicate(
                        session,
                        space_id=space_id,
                        checksum_sha256=version.checksum_sha256,
                        exclude_document_id=doc.id,
                    )
                    if other is not None:
                        raise InvalidInputError(
                            f"{doc.name!r}：这个空间里已有相同的文件「{other[1]}」，"
                            "不能再批准这一版。 "
                            f"This space already has an identical file ({other[1]}), so this "
                            "version cannot be approved."
                        )
                    new_version.apply_version_as_current(doc, version)
                    became_current.append((doc.id, previous_mime, version.mime_type))
                version.review_state = "approved" if approve else "rejected"
                version.reviewed_by_user_id = actor_user_id
                version.reviewed_at = now
                version.review_note = note or None
                changed.append((doc_id, number))
                session.add(
                    AuditEvent(
                        organization_id=organization_id,
                        actor_type="user",
                        actor_id=actor_user_id,
                        action=(
                            "document.version_approved" if approve else "document.version_rejected"
                        ),
                        resource_type="document",
                        resource_id=doc.id,
                        space_id=space_id,
                        payload={
                            "name": version.filename,
                            "version_number": number,
                            "note": note,
                        },
                    )
                )
            await session.flush()
    except IntegrityError as exc:
        if DUPLICATE_INDEX not in str(exc):
            raise
        raise InvalidInputError(
            "这个空间里刚刚出现了相同的文件，不能批准这一版。 "
            "An identical file was just added to this space, so this version cannot be approved."
        ) from exc
    for doc_id, previous_mime, mime in became_current:
        await new_version.after_current_changed(doc_id, previous_mime, mime)
    return changed


async def _check_can_become_current(
    session: AsyncSession, doc: Document, version: DocumentVersion
) -> None:
    """A version can become the current file only if the document can take one now and no
    newer version is already the current file."""

    if doc.status == "archived" or doc.review_state != "approved" or not doc.storage_key:
        raise InvalidInputError(
            f"{doc.name!r}：文档已归档或未批准，不能批准新版本。 "
            "The document is archived or not approved, so a new version cannot be approved."
        )
    if version.version_number <= await versions_service.current_version_number(session, doc):
        raise InvalidInputError(
            f"{doc.name!r}：已有更新的版本在使用，不能再批准第 {version.version_number} 版。 "
            f"A newer version is already the current file, so version {version.version_number} "
            "cannot be approved."
        )
