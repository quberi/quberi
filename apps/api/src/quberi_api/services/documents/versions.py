"""Reading and recording the versions of a document.

A document holds its files over time: version 1 is the first upload, every "upload a new
version" adds the next number. A version is written once and never changed (see
``DocumentVersion``); the document's own file columns mirror the current one.

This module is deliberately small and free of the upload/permission code in ``core`` so the
indexer and retrieval can use it without importing the whole service. The operation that
adds a version (checks, storing, dedup) is in :mod:`quberi_api.services.documents.new_version`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.documents.models import Document, DocumentVersion


async def current_version_number(session: AsyncSession, doc: Document) -> int:
    """The number of the version the document shows now (1 if it has no version rows).

    A document without a stored file, or one that predates versions and was not
    backfilled, reads as version 1: that is what its points in the index are labelled too.
    """

    if doc.current_version_id is None:
        return 1
    number = (
        await session.execute(
            select(DocumentVersion.version_number).where(
                DocumentVersion.id == doc.current_version_id
            )
        )
    ).scalar_one_or_none()
    return int(number) if number else 1


async def latest_version_numbers(
    session: AsyncSession, document_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Document id → number of its current version, for the documents that have versions."""

    ids = list(dict.fromkeys(document_ids))
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(Document.id, DocumentVersion.version_number)
            .join(DocumentVersion, DocumentVersion.id == Document.current_version_id)
            .where(Document.id.in_(ids))
        )
    ).all()
    return {doc_id: int(number) for doc_id, number in rows}


def can_see(version: DocumentVersion, *, reviewer: bool, user_id: uuid.UUID) -> bool:
    """May this person see this version? An approved one, anyone who can read the document;
    one waiting for approval or refused, only whoever added it and the space's reviewers."""

    return (
        version.review_state == "approved"
        or reviewer
        or (version.created_by_user_id is not None and version.created_by_user_id == user_id)
    )


async def version_count_by_document(
    session: AsyncSession, document_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Document id → how many approved versions it holds (only documents that hold any).

    A version waiting for approval is not part of the document's history yet."""

    ids = list(dict.fromkeys(document_ids))
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(DocumentVersion.document_id, func.count(DocumentVersion.id))
            .where(
                DocumentVersion.document_id.in_(ids), DocumentVersion.review_state == "approved"
            )
            .group_by(DocumentVersion.document_id)
        )
    ).all()
    return {doc_id: int(n) for doc_id, n in rows}


async def waiting_version_numbers(
    session: AsyncSession, document_ids: Iterable[uuid.UUID], *, user_id: uuid.UUID, reviewer: bool
) -> dict[uuid.UUID, int]:
    """Document id → the number of its version waiting for approval, for those this person may
    see (everything if ``reviewer``, otherwise what they added themselves)."""

    ids = list(dict.fromkeys(document_ids))
    if not ids:
        return {}
    stmt = select(DocumentVersion.document_id, DocumentVersion.version_number).where(
        DocumentVersion.document_id.in_(ids), DocumentVersion.review_state == "pending"
    )
    if not reviewer:
        stmt = stmt.where(DocumentVersion.created_by_user_id == user_id)
    return {doc_id: int(n) for doc_id, n in (await session.execute(stmt)).all()}


async def list_versions(session: AsyncSession, document_id: uuid.UUID) -> Sequence[DocumentVersion]:
    """All versions of a document, newest first."""

    return (
        (
            await session.execute(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document_id)
                .order_by(DocumentVersion.version_number.desc())
            )
        )
        .scalars()
        .all()
    )


async def get_version(
    session: AsyncSession, document_id: uuid.UUID, version_number: int
) -> Optional[DocumentVersion]:
    return (
        await session.execute(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document_id,
                DocumentVersion.version_number == version_number,
            )
        )
    ).scalar_one_or_none()


async def storage_keys_of(session: AsyncSession, document_id: uuid.UUID) -> list[str]:
    """Every stored object that belongs to a document, current or not."""

    return list(
        (
            await session.execute(
                select(DocumentVersion.storage_key).where(
                    DocumentVersion.document_id == document_id
                )
            )
        )
        .scalars()
        .all()
    )


async def record_first_version(
    session: AsyncSession, doc: Document, *, created_by: Optional[uuid.UUID]
) -> DocumentVersion:
    """Write version 1 for a document that has just been inserted with its first file.

    The caller has flushed ``doc``. The version row is flushed too, and only then does the
    document point at it, because the two rows reference each other.
    """

    if not doc.storage_key or not doc.checksum_sha256:
        raise ValueError("a document with no stored file has no version")
    version = DocumentVersion(
        id=uuid.uuid4(),
        document_id=doc.id,
        version_number=1,
        filename=doc.name,
        mime_type=doc.mime_type,
        size_bytes=doc.size_bytes,
        storage_key=doc.storage_key,
        checksum_sha256=doc.checksum_sha256,
        created_by_user_id=created_by,
    )
    session.add(version)
    await session.flush()
    doc.current_version_id = version.id
    await session.flush()
    return version
