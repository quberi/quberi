"""Space-scoped document endpoints.

Access model
------------
* Any caller who can see the space can list, get, and download.
* ``editor+`` on the space can upload.
* ``admin+`` or the original uploader can delete.

All authorisation delegates to
``services.documents.core._caller_space_role``, which fans out to the
Slice-B effective-binding evaluator.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.services import documents as documents_service
from quberi_api.services import guest_sandbox
from quberi_api.services.documents.errors import (
    DocumentPermissionError,
    DocumentValidationError,
    UnsupportedMimeError,
)
from quberi_api.services.rag.extract import ExtractError

router = APIRouter(prefix="/spaces", tags=["space-documents"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class DocumentView(BaseModel):
    model_config = ConfigDict(from_attributes=False)

    id: uuid.UUID
    space_id: uuid.UUID
    source_id: Optional[uuid.UUID] = None
    source_kind: str
    source_name: str
    name: str
    mime_type: str
    size_bytes: int
    checksum_sha256: Optional[str] = None
    storage_key: Optional[str] = None
    external_ref: dict = Field(default_factory=dict)
    sync_state: str
    status: str
    uploaded_by_user_id: Optional[uuid.UUID] = None
    uploader_display_name: str = ""
    created_at: datetime
    updated_at: datetime
    # RAG indexing pipeline (Slice G groundwork). Fresh uploads land as
    # ``not_indexed`` / ``chunk_count=0`` today; the RAG worker (later
    # slice) will flip these as it embeds documents.
    indexing_state: str = "not_indexed"
    chunk_count: int = 0
    last_indexed_at: Optional[datetime] = None
    indexing_error: Optional[str] = None
    # True only for a guest-sandbox file uploaded from the requesting browser
    # (matched by the hash of its X-Guest-Token). Never true for anyone else.
    is_mine: bool = False


class DocumentListFilters(BaseModel):
    """Echo of the filters actually applied, so the client can render
    "X results for term 'foo'" without keeping local state in sync."""

    q: Optional[str] = None
    source_kind: Optional[str] = None
    order_by: str = "created_at"
    order: str = "desc"
    include_archived: bool = False


class DocumentList(BaseModel):
    items: list[DocumentView]
    total: int
    limit: int
    offset: int
    filters: DocumentListFilters


class DocumentStats(BaseModel):
    """Roll-up rendered by the Knowledge grid card."""

    total: int
    total_size_bytes: int
    last_uploaded_at: Optional[datetime] = None
    by_source: dict[str, int] = Field(default_factory=dict)
    by_mime_group: dict[str, int] = Field(default_factory=dict)
    by_indexing_state: dict[str, int] = Field(default_factory=dict)


def _serialise(v, guest_token: Optional[str] = None) -> DocumentView:
    doc = v.document
    return DocumentView(
        id=doc.id,
        space_id=doc.space_id,
        source_id=doc.source_id,
        source_kind=v.source_kind,
        source_name=v.source_name,
        name=doc.name,
        mime_type=doc.mime_type,
        size_bytes=doc.size_bytes,
        checksum_sha256=doc.checksum_sha256,
        storage_key=doc.storage_key,
        external_ref=doc.external_ref or {},
        sync_state=doc.sync_state,
        status=doc.status,
        uploaded_by_user_id=doc.uploaded_by_user_id,
        uploader_display_name=v.uploader_display_name,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        indexing_state=doc.indexing_state,
        chunk_count=doc.chunk_count,
        last_indexed_at=doc.last_indexed_at,
        indexing_error=doc.indexing_error,
        is_mine=guest_sandbox.is_owned_by(doc.attributes, guest_token),
    )


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, UnsupportedMimeError):
        return HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc))
    if isinstance(exc, DocumentValidationError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, DocumentPermissionError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    raise exc


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/{space_id}/documents",
    response_model=DocumentList,
    summary="List documents in a space (paginated, searchable)",
)
async def list_documents(
    space_id: uuid.UUID,
    q: Optional[str] = Query(default=None, max_length=200),
    source_kind: Optional[str] = Query(default=None, max_length=32),
    order_by: str = Query(
        default="created_at",
        pattern="^(created_at|updated_at|name|size_bytes)$",
    ),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    include_archived: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    x_guest_token: Optional[str] = Header(default=None),
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> DocumentList:
    page = await documents_service.list_documents_in_space(
        session,
        organization_id=caller.organization.id,
        space_id=space_id,
        caller_user_id=caller.user.id,
        include_archived=include_archived,
        q=q,
        source_kind=source_kind,
        order_by=order_by,
        order=order,
        limit=limit,
        offset=offset,
    )
    return DocumentList(
        items=[_serialise(v, x_guest_token) for v in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
        filters=DocumentListFilters(
            q=q,
            source_kind=source_kind,
            order_by=order_by,
            order=order,
            include_archived=include_archived,
        ),
    )


@router.get(
    "/{space_id}/documents/stats",
    response_model=DocumentStats,
    summary="Aggregate roll-up of a space's document library",
)
async def documents_stats(
    space_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> DocumentStats:
    stats = await documents_service.stats_for_space(
        session,
        organization_id=caller.organization.id,
        space_id=space_id,
        caller_user_id=caller.user.id,
    )
    return DocumentStats(
        total=stats.total,
        total_size_bytes=stats.total_size_bytes,
        last_uploaded_at=stats.last_uploaded_at,
        by_source=stats.by_source,
        by_mime_group=stats.by_mime_group,
        by_indexing_state=stats.by_indexing_state,
    )


@router.post(
    "/{space_id}/documents",
    response_model=DocumentView,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document (multipart form)",
)
async def upload_document(
    space_id: uuid.UUID,
    file: UploadFile = File(...),
    filename: Optional[str] = Form(default=None),
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> DocumentView:
    try:
        effective_name = (filename or file.filename or "upload").strip()
        content_type = file.content_type or "application/octet-stream"
        view = await documents_service.upload_local(
            session,
            organization_id=caller.organization.id,
            space_id=space_id,
            uploader_user_id=caller.user.id,
            filename=effective_name,
            file_obj=file.file,
            content_type=content_type,
        )
    except (DocumentValidationError, DocumentPermissionError, UnsupportedMimeError) as exc:
        raise _translate(exc) from exc
    return _serialise(view)


class SaveNoteRequest(BaseModel):
    """Save a plain-text note (e.g. a snippet captured from an agent chat)."""

    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1)
    source_label: Optional[str] = Field(default=None, max_length=200)
    source_url: Optional[str] = Field(default=None, max_length=2000)


@router.post(
    "/{space_id}/notes",
    response_model=DocumentView,
    status_code=status.HTTP_201_CREATED,
    summary="Save a text note into a space (stored as a markdown document)",
)
async def save_note(
    space_id: uuid.UUID,
    body: SaveNoteRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> DocumentView:
    try:
        view = await documents_service.save_note(
            session,
            organization_id=caller.organization.id,
            space_id=space_id,
            author_user_id=caller.user.id,
            title=body.title,
            text=body.text,
            source_label=body.source_label,
            source_url=body.source_url,
        )
    except (DocumentValidationError, DocumentPermissionError, UnsupportedMimeError) as exc:
        raise _translate(exc) from exc
    return _serialise(view)


@router.get(
    "/{space_id}/documents/{document_id}",
    response_model=DocumentView,
    summary="Fetch one document's metadata",
)
async def get_document(
    space_id: uuid.UUID,
    document_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> DocumentView:
    view = await documents_service.get_document(
        session,
        document_id=document_id,
        organization_id=caller.organization.id,
        space_id=space_id,
        caller_user_id=caller.user.id,
    )
    return _serialise(view)


@router.get(
    "/{space_id}/documents/{document_id}/download",
    summary="Stream the document bytes through the API",
    responses={
        200: {
            "content": {"application/octet-stream": {}},
            "description": "The document bytes with an attachment disposition.",
        }
    },
)
async def download_document(
    space_id: uuid.UUID,
    document_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    doc, stream = await documents_service.open_download_stream(
        session,
        document_id=document_id,
        organization_id=caller.organization.id,
        space_id=space_id,
        caller_user_id=caller.user.id,
    )
    # RFC 5987 filename* for unicode-safe filenames (Chinese docs etc.).
    encoded_name = quote(doc.name)
    headers = {
        "Content-Disposition": (
            f'attachment; filename="{encoded_name}"; '
            f"filename*=UTF-8''{encoded_name}"
        ),
        "Content-Length": str(doc.size_bytes),
        "X-Document-Checksum-SHA256": doc.checksum_sha256 or "",
    }
    return StreamingResponse(
        stream,
        media_type=doc.mime_type or "application/octet-stream",
        headers=headers,
    )


_THUMBNAIL_SIDE = 320
_THUMBNAIL_MAX_SOURCE_BYTES = 26 * 1024 * 1024


@router.get(
    "/{space_id}/documents/{document_id}/thumbnail",
    summary="A small JPEG preview of a picture document",
    responses={200: {"content": {"image/jpeg": {}}}},
)
async def document_thumbnail(
    space_id: uuid.UUID,
    document_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """A downscaled copy of a picture, for result grids.

    Same access rule as the download. The preview is always *re-encoded* as a plain
    JPEG by Pillow, so whatever the stored file was (even something that only
    claims to be an image) the browser receives pixels and nothing it could run.
    """

    from quberi_api.services.ai.local_image_embedding import ImageDecodeError, prepare_image
    from quberi_api.services.rag.indexer import is_picture

    doc, stream = await documents_service.open_download_stream(
        session,
        document_id=document_id,
        organization_id=caller.organization.id,
        space_id=space_id,
        caller_user_id=caller.user.id,
    )
    if not is_picture(doc.mime_type):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not a picture")
    buf = bytearray()
    async for chunk in stream:
        buf.extend(chunk)
        if len(buf) > _THUMBNAIL_MAX_SOURCE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="picture too large"
            )

    def _render() -> bytes:
        image = prepare_image(bytes(buf))
        image.thumbnail((_THUMBNAIL_SIDE, _THUMBNAIL_SIDE))
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=80)
        return out.getvalue()

    try:
        data = await asyncio.to_thread(_render)
    except ImageDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


class DocumentTextResponse(BaseModel):
    document_id: uuid.UUID
    text: str


@router.get(
    "/{space_id}/documents/{document_id}/text",
    response_model=DocumentTextResponse,
    summary="Re-extract and return the document's full plain text",
)
async def get_document_text(
    space_id: uuid.UUID,
    document_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> DocumentTextResponse:
    # NotFoundError (no access / missing doc / reference-only source) and
    # ExtractError (unsupported format, scanned PDF, etc.) both map to
    # clean HTTP responses via the app-wide exception handlers — no
    # local try/except needed, same as every other document endpoint.
    try:
        text = await documents_service.get_document_text(
            session,
            document_id=document_id,
            organization_id=caller.organization.id,
            space_id=space_id,
            caller_user_id=caller.user.id,
        )
    except ExtractError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return DocumentTextResponse(document_id=document_id, text=text)


@router.delete(
    "/{space_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a document",
)
async def delete_document(
    space_id: uuid.UUID,
    document_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    try:
        await documents_service.delete_document(
            session,
            document_id=document_id,
            organization_id=caller.organization.id,
            space_id=space_id,
            caller_user_id=caller.user.id,
        )
    except DocumentPermissionError as exc:
        raise _translate(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Reindex — kick off a fresh indexing job for one document
# ---------------------------------------------------------------------------


class ReindexResponse(BaseModel):
    document_id: uuid.UUID
    enqueued_job_id: Optional[str]


@router.post(
    "/{space_id}/documents/{document_id}/reindex",
    response_model=ReindexResponse,
    summary="Re-run the RAG indexing pipeline for one document",
)
async def reindex_document_endpoint(
    space_id: uuid.UUID,
    document_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ReindexResponse:
    # Reuse ``get_document`` for the access check — same rules as read.
    _ = await documents_service.get_document(
        session,
        document_id=document_id,
        organization_id=caller.organization.id,
        space_id=space_id,
        caller_user_id=caller.user.id,
    )
    # Enqueue and return the job id so the UI can poll if it wants to.
    from quberi_api.workers.enqueue import enqueue_index_document

    job_id = await enqueue_index_document(document_id)
    return ReindexResponse(document_id=document_id, enqueued_job_id=job_id)


space_documents_router = router
