"""Upload a new version of an existing document.

A version is added only by someone who picked a specific document and chose "upload a new
version". Nothing here guesses that a file is a new version of another by its name or
content; an ordinary upload is always a new document.

What happens, in order:

1. The caller must be an editor or better in the space, the document must be one they may
   act on, approved, not archived, and have a stored file (a reference-only page has no
   file to replace). In a space that approves uploads only a reviewer may add a version,
   because a version that waited for approval would need a review flow of its own.
2. The file is stored under a key of its own (it can never overwrite an older version's).
3. If it is byte-identical to the current version, or to a live file of another document in
   the space, it is refused and the stored copy removed.
4. In one transaction the document row is locked, the next number is confirmed, the version
   row is written and the document's file columns are moved to it. An older version row and
   its stored file are never touched.
5. Indexing of the new current version is queued. The older versions' passages stay in the
   index under their own version number.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import BinaryIO, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document, DocumentVersion
from quberi_api.domain.identity.models import User
from quberi_api.services import doc_acl
from quberi_api.services.documents import core
from quberi_api.services.documents import review as review_service
from quberi_api.services.documents import versions as versions_service
from quberi_api.services.documents.connectors import local as local_connector
from quberi_api.services.documents.duplicates import (
    DUPLICATE_INDEX,
    find_live_duplicate,
    is_guest_upload,
)
from quberi_api.services.documents.errors import (
    DocumentPermissionError,
    DocumentValidationError,
    VersionConflictError,
)
from quberi_api.services.errors import NotFoundError
from quberi_api.workers.enqueue import enqueue_index_document

_log = logging.getLogger(__name__)

MAX_NOTE_CHARS = 500

# What indexing remembers on the document about its current file. A new file starts clean.
_FILE_BOUND_ATTRIBUTES = ("pdf_ocr", "image_text")


async def _discard(storage_key: str) -> None:
    try:
        await local_connector.delete(storage_key)
    except Exception:  # noqa: BLE001 - the refusal matters more than the cleanup
        _log.warning("could not remove the unused upload %s", storage_key, exc_info=True)


async def upload_new_version(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    document_id: uuid.UUID,
    uploader_user_id: uuid.UUID,
    filename: str,
    file_obj: BinaryIO,
    content_type: str,
    note: Optional[str] = None,
) -> core.DocumentView:
    """Add ``file_obj`` as the next version of a document and make it the current one."""

    note = (note or "").strip() or None
    if note is not None and len(note) > MAX_NOTE_CHARS:
        raise DocumentValidationError(f"the note is longer than {MAX_NOTE_CHARS} characters")

    role = await core._caller_space_role(
        session, user_id=uploader_user_id, organization_id=organization_id, space_id=space_id
    )
    if role is None:
        raise NotFoundError(f"space not accessible: {space_id}")
    if not core._at_least(role, "editor"):
        raise DocumentPermissionError(
            "editor role or higher is required to upload a new version"
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
    access = await doc_acl.load_doc_access(
        session, user_id=uploader_user_id, organization_id=organization_id
    )
    if doc.id in access.denied:
        raise NotFoundError(f"document not found: {document_id}")

    if doc.storage_key is None or doc.current_version_id is None:
        raise DocumentValidationError(
            "这份文档没有存储的文件（来自外部来源），不能上传新版本。 "
            "This document has no stored file (it comes from an external source), "
            "so it cannot take a new version."
        )
    if (
        doc.status == "archived"
        or doc.review_state != "approved"
        or is_guest_upload(doc.attributes)
    ):
        raise DocumentValidationError(
            "只有已批准、未归档的文档才能上传新版本。 "
            "A new version can only be added to a document that is approved and not archived."
        )
    if (
        await review_service.initial_state(
            session, space_id=space_id, organization_id=organization_id, role=role
        )
        != "approved"
    ):
        raise DocumentPermissionError(
            "这个空间的上传需要审核，只有空间的所有者或管理员可以上传新版本。 "
            "Uploads to this space need approval, so only an owner or admin of the space "
            "can upload a new version."
        )

    filename = core._sanitize_filename(filename)
    mime = core._validate_mime(content_type)
    core._reject_legacy_office(filename, mime)

    previous_number = int(
        (
            await session.execute(
                select(func.max(DocumentVersion.version_number)).where(
                    DocumentVersion.document_id == doc.id
                )
            )
        ).scalar_one()
        or 1
    )
    next_number = previous_number + 1
    previous_mime = doc.mime_type
    await session.commit()  # close the read transaction before writing

    storage_key = local_connector.storage_key_for(
        organization_id=str(organization_id),
        space_id=str(space_id),
        document_id=str(document_id),
        filename=filename,
        version_number=next_number,
    )
    stored = await local_connector.store(
        storage_key=storage_key,
        source_stream=file_obj,
        content_type=mime,
        max_bytes=core.MAX_UPLOAD_BYTES,
    )

    current_checksum = (
        await session.execute(select(Document.checksum_sha256).where(Document.id == doc.id))
    ).scalar_one_or_none()
    if stored.checksum_sha256 == current_checksum:
        await session.commit()
        await _discard(stored.storage_key)
        raise VersionConflictError(
            "这个文件和当前版本完全相同，没有新版本可添加。 "
            "This file is identical to the current version, so there is no new version to add."
        )
    existing = await find_live_duplicate(
        session,
        space_id=space_id,
        checksum_sha256=stored.checksum_sha256,
        exclude_document_id=doc.id,
    )
    if existing is not None:
        await core._refuse_duplicate(
            session,
            organization_id=organization_id,
            space_id=space_id,
            uploader_user_id=uploader_user_id,
            filename=filename,
            stored=stored,
            existing=existing,
        )
    await session.commit()  # close the read transaction before writing

    try:
        async with session.begin():
            # Lock the document so two new versions cannot both take the same number.
            locked = (
                await session.execute(
                    select(Document).where(Document.id == doc.id).with_for_update()
                )
            ).scalar_one()
            latest = int(
                (
                    await session.execute(
                        select(func.max(DocumentVersion.version_number)).where(
                            DocumentVersion.document_id == doc.id
                        )
                    )
                ).scalar_one()
                or 1
            )
            if latest != previous_number:
                raise VersionConflictError(
                    "另一个新版本刚刚上传，请刷新后重试。 "
                    "Another new version was just uploaded; refresh and try again."
                )
            version = DocumentVersion(
                id=uuid.uuid4(),
                document_id=doc.id,
                version_number=next_number,
                filename=filename,
                mime_type=mime,
                size_bytes=stored.size_bytes,
                storage_key=stored.storage_key,
                checksum_sha256=stored.checksum_sha256,
                note=note,
                created_by_user_id=uploader_user_id,
            )
            session.add(version)
            await session.flush()
            locked.name = filename
            locked.mime_type = mime
            locked.size_bytes = stored.size_bytes
            locked.storage_key = stored.storage_key
            locked.checksum_sha256 = stored.checksum_sha256
            locked.current_version_id = version.id
            # What indexing says about the document now describes the new file.
            locked.indexing_state = "not_indexed"
            locked.indexing_error = None
            locked.chunk_count = 0
            locked.last_indexed_at = None
            locked.attributes = {
                k: v
                for k, v in (locked.attributes or {}).items()
                if k not in _FILE_BOUND_ATTRIBUTES
            }
            session.add(
                AuditEvent(
                    organization_id=organization_id,
                    actor_type="user",
                    actor_id=uploader_user_id,
                    action="document.version_added",
                    resource_type="document",
                    resource_id=doc.id,
                    space_id=space_id,
                    payload={
                        "name": filename,
                        "version_number": next_number,
                        "previous_version_number": previous_number,
                        "mime": mime,
                        "size_bytes": stored.size_bytes,
                        "checksum_sha256": stored.checksum_sha256,
                        "note": note or "",
                    },
                )
            )
            await session.flush()
            await session.refresh(locked, attribute_names=["created_at", "updated_at"])
    except VersionConflictError:
        await _discard(stored.storage_key)
        raise
    except IntegrityError as exc:
        # An identical file was uploaded to the space between the check and the write.
        await _discard(stored.storage_key)
        if DUPLICATE_INDEX not in str(exc):
            raise
        existing = await find_live_duplicate(
            session,
            space_id=space_id,
            checksum_sha256=stored.checksum_sha256,
            exclude_document_id=doc.id,
        )
        if existing is None:
            raise
        raise VersionConflictError(
            "这个空间里刚刚上传了相同的文件。 An identical file was just uploaded to this space."
        ) from exc

    await _after_commit(doc.id, organization_id, previous_mime, mime)
    return await core._to_view(session, locked)


async def _after_commit(
    document_id: uuid.UUID, organization_id: uuid.UUID, previous_mime: str, mime: str
) -> None:
    """Queue indexing of the new file, and drop the picture vector of an old picture.

    A picture is found by what it shows, kept in a collection of its own that holds one
    vector per document. When the document stops being a picture, the old vector would
    keep pointing at a document that now shows something else.
    """

    from quberi_api.services.rag.indexer import is_picture

    await enqueue_index_document(document_id)
    if is_picture(previous_mime) and not is_picture(mime):
        try:
            from quberi_api.core.config import get_settings
            from quberi_api.infrastructure.qdrant import create_qdrant_client
            from quberi_api.services.rag.indexer import remove_image_from_index

            qdrant = create_qdrant_client(get_settings())
            try:
                await remove_image_from_index(qdrant=qdrant, document_id=document_id)
            finally:
                await qdrant.close()
        except Exception:  # noqa: BLE001 - a stale vector is dropped by the search's own check
            _log.warning("could not remove the picture vector of %s", document_id, exc_info=True)


# ---------------------------------------------------------------------------
# Reading the history
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VersionView:
    version: DocumentVersion
    created_by_name: str
    is_current: bool


async def _readable_document(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    caller_user_id: uuid.UUID,
) -> Document:
    """The document, if this person may read it (the same rule as opening it)."""

    whole_space, access, review = await core._read_gate(
        session, user_id=caller_user_id, organization_id=organization_id, space_id=space_id
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
    core._require_visible(doc, whole_space=whole_space, access=access, review=review)
    return doc


async def list_document_versions(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    caller_user_id: uuid.UUID,
) -> list[VersionView]:
    """Every version of a document, newest first. Same access as reading the document."""

    doc = await _readable_document(
        session,
        document_id=document_id,
        organization_id=organization_id,
        space_id=space_id,
        caller_user_id=caller_user_id,
    )
    rows = (
        await session.execute(
            select(DocumentVersion, User.display_name)
            .outerjoin(User, User.id == DocumentVersion.created_by_user_id)
            .where(DocumentVersion.document_id == doc.id)
            .order_by(DocumentVersion.version_number.desc())
        )
    ).all()
    return [
        VersionView(
            version=v,
            created_by_name=name or "",
            is_current=v.id == doc.current_version_id,
        )
        for v, name in rows
    ]


async def open_version_stream(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    version_number: int,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    caller_user_id: uuid.UUID,
) -> tuple[DocumentVersion, AsyncIterator[bytes]]:
    """The stored file of one version, for download. Same access as the current file."""

    doc = await _readable_document(
        session,
        document_id=document_id,
        organization_id=organization_id,
        space_id=space_id,
        caller_user_id=caller_user_id,
    )
    version = await versions_service.get_version(session, doc.id, version_number)
    if version is None:
        raise NotFoundError(f"version not found: {version_number}")
    return version, local_connector.open_stream(version.storage_key)
