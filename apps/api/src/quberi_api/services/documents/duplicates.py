"""Finding an identical file already in a space.

Two uploads are "the same file" when their SHA-256 matches. Only within one space: the
same file in two spaces is two separate pieces of knowledge. Kept apart from ``core``
because both the upload and the review code need it.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.documents.models import Document

# A copy only counts while it is waiting for approval or approved: a rejected copy does
# not block uploading the file again.
LIVE_REVIEW_STATES = ("approved", "pending")
# Name of the unique index that backs the check (see migration 0035).
DUPLICATE_INDEX = "uq_documents_space_checksum_live"


def is_guest_upload(attributes: Optional[dict]) -> bool:
    """Guest sandbox uploads share one space, so they are never refused as duplicates."""

    return bool((attributes or {}).get("guest_owner"))


async def find_live_duplicate(
    session: AsyncSession,
    *,
    space_id: uuid.UUID,
    checksum_sha256: Optional[str],
    exclude_document_id: Optional[uuid.UUID] = None,
) -> Optional[tuple[uuid.UUID, str, str]]:
    """The uploaded file in this space with the same SHA-256 that still counts.

    Returns ``(id, name, review_state)`` or ``None``. It looks at uploaded files only (a
    reference-only document has no stored file) and ignores guest sandbox uploads,
    archived and rejected copies. The caller decides what the person may be told.
    """

    if not checksum_sha256:
        return None
    stmt = select(Document.id, Document.name, Document.review_state).where(
        Document.space_id == space_id,
        Document.checksum_sha256 == checksum_sha256,
        Document.storage_key.is_not(None),
        Document.status != "archived",
        Document.review_state.in_(LIVE_REVIEW_STATES),
        Document.attributes.op("->>")("guest_owner").is_(None),
    )
    if exclude_document_id is not None:
        stmt = stmt.where(Document.id != exclude_document_id)
    row = (await session.execute(stmt.limit(1))).first()
    return (row[0], row[1], row[2]) if row else None
