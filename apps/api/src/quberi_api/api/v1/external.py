"""Knowledge access for outside tools (bearer = an OAuth access token).

* POST /api/v1/external/search - search the knowledge the grant's owner can read.

Everything the web app enforces still applies, because this calls the same
retrieval function the chat uses: spaces are re-computed from the person's
*current* permissions on every call, then filtered inside the vector store.

What is different, on purpose:

* the results leave Quberi without the "check before sending" step, so the
  organisation must have switched outside tools on (checked on every call);
* every call is audited with the tool's name and the documents returned, and
  rate limited per grant.
"""

from __future__ import annotations

import io
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session, get_infrastructure
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.spaces.models import Space
from quberi_api.infrastructure.registry import Infrastructure
from quberi_api.services import documents as documents_service
from quberi_api.services import spaces as spaces_service
from quberi_api.services.documents import external_upload
from quberi_api.services.documents.errors import (
    DocumentPermissionError,
    DocumentValidationError,
    UnsupportedMimeError,
)
from quberi_api.services.errors import NotFoundError
from quberi_api.services.oauth import clients, core
from quberi_api.services.rag.retrieve import retrieve_for_user
from quberi_api.services.retrieval_audit import document_ids_from_chunks

router = APIRouter(prefix="/external", tags=["external"])

_bearer = HTTPBearer(auto_error=False)
_SEARCH_RATE_LIMIT = 60  # per grant per minute
_SAVE_RATE_LIMIT = 30  # per grant per minute — writes are rarer than searches
_UPLOAD_RATE_LIMIT = 10  # per grant per minute — files are heavier than notes
_MAX_PASSAGE_CHARS = 4_000


async def require_external_caller(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    session: AsyncSession = Depends(get_db_session),
) -> core.ExternalCaller:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing access token",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    try:
        return await core.authenticate_access_token(session, credentials.credentials)
    except core.ExternalAuthError as exc:
        headers = (
            {"WWW-Authenticate": f'Bearer error="{exc.code}"'}
            if exc.status == 401
            else None
        )
        raise HTTPException(
            status_code=exc.status,
            detail={"code": exc.code, "message": exc.detail},
            headers=headers,
        ) from exc


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=10)


class Passage(BaseModel):
    document_id: uuid.UUID
    document_name: str
    space_id: uuid.UUID
    space_name: str
    chunk_index: int
    text: str
    score: float


class SearchResponse(BaseModel):
    organization: str
    passages: list[Passage]
    searched_spaces: int


@router.post("/search", response_model=SearchResponse, summary="Search readable knowledge")
async def search(
    payload: SearchRequest,
    ext: core.ExternalCaller = Depends(require_external_caller),
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> SearchResponse:
    if not ext.has_scope(clients.SCOPE_KNOWLEDGE_READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "insufficient_scope", "message": "knowledge:read is required"},
        )
    try:
        await core.rate_limit(
            infra.redis,
            key=f"quberi:external:search:{ext.grant_id}",
            limit=_SEARCH_RATE_LIMIT,
            window_seconds=60,
        )
    except core.OAuthError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail={"code": exc.error, "message": exc.description},
            headers={"Retry-After": "60"} if exc.status == 429 else None,
        ) from exc

    result = await retrieve_for_user(
        session,
        qdrant=infra.qdrant,
        organization_id=ext.organization.id,
        caller_user_id=ext.user.id,
        query=payload.query,
        top_k=payload.top_k,
    )
    chunks = result.chunks[: payload.top_k]

    space_ids = {c.space_id for c in chunks}
    names: dict[uuid.UUID, str] = {}
    if space_ids:
        rows = await session.execute(
            select(Space.id, Space.name).where(
                Space.id.in_(space_ids),
                Space.organization_id == ext.organization.id,
            )
        )
        names = {sid: name for sid, name in rows.all()}
    # Close the read transaction the retrieval above opened before writing.
    await session.commit()

    async with session.begin():
        session.add(
            AuditEvent(
                organization_id=ext.organization.id,
                actor_type="user",
                actor_id=ext.user.id,
                action="knowledge.external_search",
                resource_type="oauth_grant",
                resource_id=ext.grant_id,
                payload={
                    "grant_id": str(ext.grant_id),
                    "client_name": ext.client_name,
                    "result_count": len(chunks),
                    # Ids only, like chat answers: enough to look each one up.
                    "retrieved_document_ids": document_ids_from_chunks(chunks),
                },
            )
        )

    return SearchResponse(
        organization=ext.organization.name,
        searched_spaces=len(result.space_ids),
        passages=[
            Passage(
                document_id=c.document_id,
                document_name=c.document_name,
                space_id=c.space_id,
                space_name=names.get(c.space_id, ""),
                chunk_index=c.chunk_index,
                text=c.text[:_MAX_PASSAGE_CHARS],
                score=c.score,
            )
            for c in chunks
        ],
    )


# ---------------------------------------------------------------------------
# Save a note into the person's knowledge (requires knowledge:write)
# ---------------------------------------------------------------------------


class SaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1)
    # Named in words, not an id: the tool/model refers to a space the way the
    # person does. Omit to use the person's personal space.
    space: Optional[str] = Field(default=None, max_length=200)
    source_label: Optional[str] = Field(default=None, max_length=200)
    source_url: Optional[str] = Field(default=None, max_length=2000)


_MAX_SPACE_CHOICES = 20


async def _resolve_space(
    session: AsyncSession, ext: core.ExternalCaller, name: Optional[str]
) -> spaces_service.SpaceView:
    """Pick the space a save or upload goes to, or answer with a clear hint.

    With no name, the target is the person's personal space. If they have
    none, say so (409 ``space_required``) and list the spaces they can reach so
    the tool can ask which one - never drop the content into a shared space
    the person did not choose.
    """

    try:
        return await spaces_service.resolve_target_space(
            session,
            organization_id=ext.organization.id,
            user_id=ext.user.id,
            name=name,
        )
    except spaces_service.PersonalSpaceMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "space_required",
                "message": "You have no personal space; say which space to use.",
                "spaces": exc.candidates[:_MAX_SPACE_CHOICES],
            },
        ) from exc
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "space_not_found", "message": str(exc)},
        ) from exc


class SaveResponse(BaseModel):
    document_id: uuid.UUID
    space_id: uuid.UUID
    space_name: str
    name: str


@router.post(
    "/save",
    response_model=SaveResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Save a note into readable knowledge",
)
async def save(
    payload: SaveRequest,
    ext: core.ExternalCaller = Depends(require_external_caller),
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> SaveResponse:
    if not ext.has_scope(clients.SCOPE_KNOWLEDGE_WRITE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "insufficient_scope", "message": "knowledge:write is required"},
        )
    try:
        await core.rate_limit(
            infra.redis,
            key=f"quberi:external:save:{ext.grant_id}",
            limit=_SAVE_RATE_LIMIT,
            window_seconds=60,
        )
    except core.OAuthError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail={"code": exc.error, "message": exc.description},
            headers={"Retry-After": "60"} if exc.status == 429 else None,
        ) from exc

    # Resolve the target space from a human-friendly name (or the personal
    # default). The write permission itself is re-checked inside save_note.
    target = await _resolve_space(session, ext, payload.space)
    # resolve_target_space opened a read transaction; close it before writing.
    await session.commit()

    try:
        view = await documents_service.save_note(
            session,
            organization_id=ext.organization.id,
            space_id=target.space.id,
            author_user_id=ext.user.id,
            title=payload.title,
            text=payload.text,
            source_label=payload.source_label or ext.client_name,
            source_url=payload.source_url,
        )
    except DocumentPermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": str(exc)},
        ) from exc
    except (DocumentValidationError, UnsupportedMimeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_note", "message": str(exc)},
        ) from exc

    # save_note already committed the document; record the audit event on the
    # session's current transaction rather than opening a nested one (the
    # session has an open transaction here, so ``session.begin()`` would clash).
    session.add(
        AuditEvent(
            organization_id=ext.organization.id,
            actor_type="user",
            actor_id=ext.user.id,
            action="knowledge.external_save",
            resource_type="oauth_grant",
            resource_id=ext.grant_id,
            payload={
                "grant_id": str(ext.grant_id),
                "client_name": ext.client_name,
                "document_id": str(view.document.id),
                "space_id": str(target.space.id),
            },
        )
    )
    await session.commit()

    return SaveResponse(
        document_id=view.document.id,
        space_id=target.space.id,
        space_name=target.space.name,
        name=view.document.name,
    )


# ---------------------------------------------------------------------------
# Upload a file into the person's knowledge (requires knowledge:upload)
# ---------------------------------------------------------------------------


class UploadResponse(BaseModel):
    document_id: uuid.UUID
    space_id: uuid.UUID
    space_name: str
    name: str
    mime_type: str
    size_bytes: int


def _too_large(limit: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail={
            "code": "file_too_large",
            "message": f"the file is larger than the {limit}-byte limit",
        },
    )


async def _read_capped(upload: UploadFile, limit: int) -> bytes:
    """Read the file in pieces and stop as soon as it passes ``limit``."""

    buf = bytearray()
    while True:
        piece = await upload.read(1024 * 1024)
        if not piece:
            break
        buf.extend(piece)
        if len(buf) > limit:
            raise _too_large(limit)
    return bytes(buf)


@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file into readable knowledge",
)
async def upload(
    request: Request,
    file: UploadFile = File(...),
    space: Optional[str] = Form(default=None, max_length=200),
    ext: core.ExternalCaller = Depends(require_external_caller),
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> UploadResponse:
    # Scope first, before touching the body at all.
    if not ext.has_scope(clients.SCOPE_KNOWLEDGE_UPLOAD):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "insufficient_scope", "message": "knowledge:upload is required"},
        )
    try:
        await core.rate_limit(
            infra.redis,
            key=f"quberi:external:upload:{ext.grant_id}",
            limit=_UPLOAD_RATE_LIMIT,
            window_seconds=60,
        )
    except core.OAuthError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail={"code": exc.error, "message": exc.description},
            headers={"Retry-After": "60"} if exc.status == 429 else None,
        ) from exc

    limit = documents_service.MAX_UPLOAD_BYTES
    # Refuse an honest oversized request without reading it; a dishonest one is
    # caught by the capped read below.
    declared_length = request.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > limit + 64 * 1024:
        raise _too_large(limit)

    data = await _read_capped(file, limit)
    try:
        checked = external_upload.check_upload(
            data=data,
            filename=file.filename or "",
            declared_mime=file.content_type or "",
        )
    except UnsupportedMimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"code": "unsupported_type", "message": str(exc)},
        ) from exc
    except DocumentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_file", "message": str(exc)},
        ) from exc

    target = await _resolve_space(session, ext, space)
    # resolve_target_space opened a read transaction; close it before writing.
    await session.commit()

    try:
        view = await documents_service.upload_local(
            session,
            organization_id=ext.organization.id,
            space_id=target.space.id,
            uploader_user_id=ext.user.id,
            filename=checked.filename,
            file_obj=io.BytesIO(data),
            content_type=checked.mime,
            attributes={"origin": "external_upload", "source_label": ext.client_name},
        )
    except DocumentPermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": str(exc)},
        ) from exc
    except UnsupportedMimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"code": "unsupported_type", "message": str(exc)},
        ) from exc
    except DocumentValidationError as exc:
        status_code = (
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            if "limit" in str(exc)
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(
            status_code=status_code,
            detail={"code": "invalid_file", "message": str(exc)},
        ) from exc

    # upload_local already committed the document and its own audit row; this
    # one records that it came through an outside tool, and which one.
    session.add(
        AuditEvent(
            organization_id=ext.organization.id,
            actor_type="user",
            actor_id=ext.user.id,
            action="knowledge.external_upload",
            resource_type="oauth_grant",
            resource_id=ext.grant_id,
            payload={
                "grant_id": str(ext.grant_id),
                "client_name": ext.client_name,
                "document_id": str(view.document.id),
                "space_id": str(target.space.id),
                "mime": checked.mime,
                "size_bytes": len(data),
            },
        )
    )
    await session.commit()

    return UploadResponse(
        document_id=view.document.id,
        space_id=target.space.id,
        space_name=target.space.name,
        name=view.document.name,
        mime_type=checked.mime,
        size_bytes=len(data),
    )


external_router = router
