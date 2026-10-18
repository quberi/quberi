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

import base64
import binascii
import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session, get_infrastructure
from quberi_api.core.config import get_settings
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document
from quberi_api.domain.identity.models import Organization, User
from quberi_api.domain.oauth.models import OAuthGrant
from quberi_api.domain.spaces.models import Space
from quberi_api.infrastructure.registry import Infrastructure
from quberi_api.services import documents as documents_service
from quberi_api.services import spaces as spaces_service
from quberi_api.services.doc_acl import NO_DOC_ACCESS, DocAccess, load_doc_access, visibility_clause
from quberi_api.services.documents import core as documents_core
from quberi_api.services.documents import external_upload
from quberi_api.services.documents.errors import (
    DocumentPermissionError,
    DocumentValidationError,
    UnsupportedMimeError,
)
from quberi_api.services.errors import NotFoundError
from quberi_api.services.oauth import clients, core
from quberi_api.services import external_image_links
from quberi_api.services.documents.thumbnail import ThumbnailError, render_thumbnail
from quberi_api.services.rag import image_search
from quberi_api.services.rag.indexer import is_picture
from quberi_api.services.rag.retrieve import retrieve_for_user
from quberi_api.services.retrieval_audit import document_ids_from_chunks

router = APIRouter(prefix="/external", tags=["external"])

_bearer = HTTPBearer(auto_error=False)
_SEARCH_RATE_LIMIT = 60  # per grant per minute
_SAVE_RATE_LIMIT = 30  # per grant per minute — writes are rarer than searches
_UPLOAD_RATE_LIMIT = 10  # per grant per minute — files are heavier than notes
_MAX_PASSAGE_CHARS = 4_000
# How many still-indexing documents a search response names, and how recently
# one must have changed to count (a stuck or abandoned row is not "in progress").
_MAX_PENDING_LISTED = 10
_PENDING_WINDOW = timedelta(hours=2)
_PENDING_STATES = ("not_indexed", "indexing")
_LIST_RATE_LIMIT = 60  # per grant per minute, shared by both listing endpoints
_MAX_SPACES_LISTED = 100
_MAX_DOCUMENTS_PER_PAGE = 50


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
    # Name of one space to look in; omit to look in every space the person can read.
    space: Optional[str] = Field(default=None, max_length=200)
    # What to look in: ``both`` (default) the documents' text and, beside it, pictures that
    # might be what was asked for; ``text`` only the text; ``image`` only the pictures.
    kind: Literal["text", "image", "both"] = "both"


class Passage(BaseModel):
    document_id: uuid.UUID
    document_name: str
    space_id: uuid.UUID
    space_name: str
    chunk_index: int
    text: str
    score: float


class PendingDocument(BaseModel):
    """A document in a searched space that cannot be found yet."""

    document_name: str
    space_name: str
    # 'not_indexed' (queued), 'indexing' (being read right now), or 'awaiting_approval' (the
    # space approves uploads and this one, the caller's own, has not been approved yet).
    state: str


class ImageResult(BaseModel):
    name: str
    space_name: str
    mime_type: str
    size_bytes: int
    # 0..1 closeness; only useful for ordering and for judging a weak match.
    score: float
    # Absolute link to a small JPEG preview, valid for ``preview_expires_in`` seconds.
    # Null when this server has no signing key (the picture is still listed).
    preview_url: Optional[str] = None


class SearchResponse(BaseModel):
    organization: str
    passages: list[Passage]
    searched_spaces: int
    # Documents in the spaces just searched that are still being indexed, so a
    # tool can say "that file is still processing" instead of presenting
    # unrelated passages as the answer. Newest first, capped; the total is
    # reported separately.
    pending_documents: list[PendingDocument] = Field(default_factory=list)
    pending_total: int = 0
    # Pictures that might be what was asked for (``kind`` both or image). With ``both`` they
    # are only the few that score well enough and are *possible* matches, to be offered
    # beside the passages; with ``image`` they are simply the closest ones. Defined below.
    images: list[ImageResult] = Field(default_factory=list)
    preview_expires_in: int = 0


async def _requested_spaces(
    session: AsyncSession, ext: core.ExternalCaller, name: Optional[str]
) -> Optional[list[uuid.UUID]]:
    """The one space a request names (404 if the person cannot see it), or ``None`` for all."""

    if name is None or not name.strip():
        return None
    try:
        view = await spaces_service.resolve_target_space(
            session,
            organization_id=ext.organization.id,
            user_id=ext.user.id,
            name=name,
            include_shared=True,
        )
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "space_not_found", "message": str(exc)},
        ) from exc
    return [view.space.id]


async def _image_rate_limit(infra: Infrastructure, ext: core.ExternalCaller) -> None:
    """Each picture lookup runs the picture model, so it has a limit of its own."""

    await core.rate_limit(
        infra.redis,
        key=f"quberi:external:imagesearch:{ext.grant_id}",
        limit=_IMAGE_SEARCH_RATE_LIMIT,
        window_seconds=60,
    )


def _preview_link(request: Request, ext: core.ExternalCaller, document_id: uuid.UUID) -> Optional[str]:
    settings = get_settings()
    if not external_image_links.links_available(settings):
        return None
    token = external_image_links.sign(
        settings, user_id=ext.user.id, grant_id=ext.grant_id, document_id=document_id
    )
    return f"{_public_origin(request)}/api/v1/external/preview/{token}.jpg"


async def _related_pictures(
    request: Request,
    session: AsyncSession,
    infra: Infrastructure,
    ext: core.ExternalCaller,
    *,
    query: str,
    requested: Optional[list[uuid.UUID]],
) -> list[tuple[image_search.ImageMatch, ImageResult]]:
    """A few pictures that might be what a text search was after, or ``[]``.

    A side lookup: it never fails the search it rides along with. It is skipped for a long
    query, when there is no picture to find, when picture search is off, and when the picture
    limit is reached; only pictures scoring at least the chat's floor are kept.
    """

    from quberi_api.services.conversations import (
        RELATED_PICTURE_MAX_CHARS,
        RELATED_PICTURE_MIN_SCORE,
        RELATED_PICTURE_TOP_K,
    )

    text = query.strip()
    if not text or len(text) > RELATED_PICTURE_MAX_CHARS or not get_settings().image_search_enabled:
        return []
    try:
        await _image_rate_limit(infra, ext)
        # A SAVEPOINT: a failure undoes only this lookup. A plain rollback would expire the
        # caller's objects (the grant, the person) and break the search it rides along with.
        async with session.begin_nested():
            scope = await image_search.resolve_image_access(
                session,
                organization_id=ext.organization.id,
                caller_user_id=ext.user.id,
                requested_space_ids=requested,
            )
            if not await image_search.has_searchable_pictures(
                session,
                organization_id=ext.organization.id,
                space_ids=scope.space_ids,
                doc_access=scope.doc_access,
            ):
                return []
            vector = await image_search.embed_query(image=None, text=text)
            found = await image_search.search_images(
                session,
                qdrant=infra.qdrant,
                organization_id=ext.organization.id,
                space_ids=scope.space_ids,
                query_vector=vector,
                top_k=RELATED_PICTURE_TOP_K,
                doc_access=scope.doc_access,
            )
    except Exception:  # noqa: BLE001 - a side lookup must never break the search
        import logging

        logging.getLogger(__name__).warning("related pictures failed", exc_info=True)
        return []
    return [
        (m, _image_result(request, ext, m))
        for m in found.matches
        if m.score >= RELATED_PICTURE_MIN_SCORE
    ]


def _image_result(request: Request, ext: core.ExternalCaller, m: image_search.ImageMatch) -> ImageResult:
    return ImageResult(
        name=m.document_name,
        space_name=m.space_name,
        mime_type=m.mime_type,
        size_bytes=m.size_bytes,
        score=round(m.score, 4),
        preview_url=_preview_link(request, ext, m.document_id),
    )


@router.post("/search", response_model=SearchResponse, summary="Search readable knowledge")
async def search(
    payload: SearchRequest,
    request: Request,
    ext: core.ExternalCaller = Depends(require_external_caller),
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> SearchResponse:
    if not ext.has_scope(clients.SCOPE_KNOWLEDGE_READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "insufficient_scope", "message": "knowledge:read is required"},
        )
    if payload.kind == "image":
        # Pictures only: the same lookup, limits and audit as the dedicated endpoint.
        if len(payload.query) > image_search.MAX_QUERY_TEXT_CHARS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "bad_query",
                    "message": f"describe the picture in at most {image_search.MAX_QUERY_TEXT_CHARS} characters",
                },
            )
        pictures = await search_images_by_description(
            ImageSearchRequest(query=payload.query, top_k=payload.top_k, space=payload.space),
            request,
            ext,
            session,
            infra,
        )
        return SearchResponse(
            organization=pictures.organization,
            passages=[],
            searched_spaces=pictures.searched_spaces,
            images=pictures.images,
            preview_expires_in=pictures.preview_expires_in,
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

    requested = await _requested_spaces(session, ext, payload.space)
    result = await retrieve_for_user(
        session,
        qdrant=infra.qdrant,
        organization_id=ext.organization.id,
        caller_user_id=ext.user.id,
        query=payload.query,
        top_k=payload.top_k,
        space_ids=requested,
    )
    chunks = result.chunks[: payload.top_k]

    space_ids = {c.space_id for c in chunks}
    # Only spaces this person can read (the retrieval's own live scope), so
    # naming a pending document tells them nothing a space listing would not.
    pending_total, pending_rows = await _pending_documents(
        session,
        organization_id=ext.organization.id,
        readable_space_ids=result.space_ids,
        doc_access=result.doc_access,
        user_id=ext.user.id,
    )
    names: dict[uuid.UUID, str] = {}
    wanted_names = space_ids | {sid for _name, sid, _state in pending_rows}
    if wanted_names:
        rows = await session.execute(
            select(Space.id, Space.name).where(
                Space.id.in_(wanted_names),
                Space.organization_id == ext.organization.id,
            )
        )
        names = {sid: name for sid, name in rows.all()}
    # Pictures that might be what was asked for (the text above cannot see pictures).
    related = (
        await _related_pictures(request, session, infra, ext, query=payload.query, requested=requested)
        if payload.kind == "both"
        else []
    )
    # Close the read transaction the retrieval above opened before writing.
    await session.commit()

    audit_payload = {
        "grant_id": str(ext.grant_id),
        "client_name": ext.client_name,
        "result_count": len(chunks),
        # Ids only, like chat answers: enough to look each one up.
        "retrieved_document_ids": document_ids_from_chunks(chunks),
    }
    if related:
        audit_payload["related_picture_ids"] = [str(m.document_id) for m, _r in related]
    async with session.begin():
        session.add(
            AuditEvent(
                organization_id=ext.organization.id,
                actor_type="user",
                actor_id=ext.user.id,
                action="knowledge.external_search",
                resource_type="oauth_grant",
                resource_id=ext.grant_id,
                payload=audit_payload,
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
        pending_documents=[
            PendingDocument(
                document_name=name,
                space_name=names.get(space_id, ""),
                state=state,
            )
            for name, space_id, state in pending_rows
        ],
        pending_total=pending_total,
        images=[r for _m, r in related],
        preview_expires_in=external_image_links.LINK_TTL_SECONDS if related else 0,
    )


async def _pending_documents(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    readable_space_ids: list[uuid.UUID],
    doc_access: DocAccess = NO_DOC_ACCESS,
    user_id: uuid.UUID | None = None,
) -> tuple[int, list[tuple[str, uuid.UUID, str]]]:
    """Documents in these spaces that are not searchable yet.

    Recently changed ones that are being indexed, plus (for ``user_id``) their own uploads
    that are waiting for approval, reported as ``awaiting_approval``: without that a file
    that will never be indexed until someone approves it would look like it is still being
    processed. Returns the total and the newest few as plain ``(name, space_id, state)``
    tuples (not ORM rows: the session is committed before the response is built).
    """

    if not readable_space_ids and not doc_access.granted:
        return 0, []
    cutoff = datetime.now(timezone.utc) - _PENDING_WINDOW
    where = (
        Document.organization_id == organization_id,
        # Whole spaces, plus single documents shared with the person; never a hidden one.
        or_(
            Document.space_id.in_(readable_space_ids),
            Document.id.in_(set(doc_access.granted)),
        ),
        Document.id.not_in(doc_access.denied),
        Document.status != "archived",
        or_(
            and_(
                Document.review_state == "approved",
                Document.indexing_state.in_(_PENDING_STATES),
                Document.updated_at >= cutoff,
            ),
            and_(
                Document.review_state == "pending",
                user_id is not None and Document.uploaded_by_user_id == user_id,
            ),
        ),
    )
    total = (
        await session.execute(select(func.count()).select_from(Document).where(*where))
    ).scalar_one()
    if total == 0:
        return 0, []
    rows = await session.execute(
        select(Document.name, Document.space_id, Document.indexing_state, Document.review_state)
        .where(*where)
        .order_by(Document.updated_at.desc())
        .limit(_MAX_PENDING_LISTED)
    )
    return int(total), [
        (name, sid, "awaiting_approval" if review == "pending" else state)
        for name, sid, state, review in rows.all()
    ]


# ---------------------------------------------------------------------------
# Browse: which spaces, and which documents in one (requires knowledge:read)
# ---------------------------------------------------------------------------
#
# Metadata only. Neither endpoint returns any document text (search is the one
# path for that) or who uploaded what. Both read through the same live access
# rules as search: a space or document the person cannot see in Quberi is not
# listed here.


class SpaceInfo(BaseModel):
    name: str
    # More than 0 when the person cannot read this space as a whole and sees only that
    # many documents of it, each shared with them individually. Then ``document_count``
    # is the same number.
    shared_only_documents: int = 0
    # 'personal' (the person's own space) or 'shared'.
    kind: str
    # Whether save and upload would be allowed there (editor or above).
    can_edit: bool
    document_count: int


class SpacesResponse(BaseModel):
    organization: str
    spaces: list[SpaceInfo]
    # How many spaces the person can read in all (the list is capped).
    total: int


class DocumentInfo(BaseModel):
    name: str
    mime_type: str
    size_bytes: int
    updated_at: datetime
    # 'ready' (searchable), 'indexing' (being processed now),
    # 'awaiting_approval' / 'rejected' (the space approves uploads; this one is the caller's own
    # and has not been approved, so it is not being indexed) or
    # 'not_searchable' (processing failed or never finished, or a kind of file
    # that cannot be searched by content, such as an image).
    status: str
    # Why a reviewer rejected it. Set only on the caller's own rejected files, so the person who
    # uploaded (or the tool acting for them) can find out why it is not searchable.
    review_note: Optional[str] = None


class DocumentsResponse(BaseModel):
    space_name: str
    documents: list[DocumentInfo]
    # Documents in this space in all, so a tool can say "50 of 212".
    total: int
    # Pass back as ``cursor`` for the next page; null on the last page.
    next_cursor: Optional[str] = None
    # The kind filter that was applied, echoed so a tool can tell a server that
    # honoured it from an older one that ignored the parameter.
    type: Optional[str] = None


async def _list_rate_limit(infra: Infrastructure, ext: core.ExternalCaller) -> None:
    try:
        await core.rate_limit(
            infra.redis,
            key=f"quberi:external:list:{ext.grant_id}",
            limit=_LIST_RATE_LIMIT,
            window_seconds=60,
        )
    except core.OAuthError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail={"code": exc.error, "message": exc.description},
            headers={"Retry-After": "60"} if exc.status == 429 else None,
        ) from exc


def _require_read(ext: core.ExternalCaller) -> None:
    if not ext.has_scope(clients.SCOPE_KNOWLEDGE_READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "insufficient_scope", "message": "knowledge:read is required"},
        )


def _public_status(
    state: str, updated_at: datetime, now: datetime, review_state: str = "approved"
) -> str:
    # A file waiting for (or refused) approval is not "being indexed": say what it is waiting for.
    if review_state == "pending":
        return "awaiting_approval"
    if review_state == "rejected":
        return "rejected"
    if state == "indexed":
        return "ready"
    if state in _PENDING_STATES and now - updated_at <= _PENDING_WINDOW:
        return "indexing"
    return "not_searchable"


def _encode_cursor(updated_at: datetime, doc_id: uuid.UUID) -> str:
    raw = json.dumps({"t": updated_at.isoformat(), "i": str(doc_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        when = datetime.fromisoformat(data["t"])
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when, uuid.UUID(data["i"])
    except (ValueError, KeyError, TypeError, binascii.Error, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_cursor", "message": "the page cursor is not valid"},
        ) from exc


async def _audit_listing(
    session: AsyncSession, ext: core.ExternalCaller, **payload: object
) -> None:
    # The read transaction is still open; close it before writing the audit row.
    await session.commit()
    async with session.begin():
        session.add(
            AuditEvent(
                organization_id=ext.organization.id,
                actor_type="user",
                actor_id=ext.user.id,
                action="knowledge.external_list",
                resource_type="oauth_grant",
                resource_id=ext.grant_id,
                payload={"grant_id": str(ext.grant_id), "client_name": ext.client_name, **payload},
            )
        )


@router.get("/spaces", response_model=SpacesResponse, summary="List the spaces the person can read")
async def list_spaces(
    ext: core.ExternalCaller = Depends(require_external_caller),
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> SpacesResponse:
    _require_read(ext)
    await _list_rate_limit(infra, ext)

    views = [
        v
        for v in await spaces_service.list_spaces_for_user(
            session,
            organization_id=ext.organization.id,
            user_id=ext.user.id,
            include_shared=True,
        )
        if v.space.status == "active"
    ]
    shown = views[:_MAX_SPACES_LISTED]
    counts: dict[uuid.UUID, int] = {}
    if shown:
        access = await load_doc_access(
            session, user_id=ext.user.id, organization_id=ext.organization.id
        )
        whole_ids = [v.space.id for v in shown if v.shared_only_documents == 0]
        # The same rule as the document list: a space read as a whole minus the documents
        # hidden from the person, and the individually shared documents of the others.
        rows = await session.execute(
            select(Document.space_id, func.count())
            .where(
                Document.organization_id == ext.organization.id,
                Document.status != "archived",
                or_(
                    Document.review_state == "approved",
                    Document.uploaded_by_user_id == ext.user.id,
                ),
                or_(
                    and_(
                        Document.space_id.in_(whole_ids),
                        Document.id.not_in(access.denied),
                    ),
                    and_(
                        Document.space_id.in_([v.space.id for v in shown]),
                        Document.id.in_(set(access.granted)),
                    ),
                ),
            )
            .group_by(Document.space_id)
        )
        counts = {sid: int(n) for sid, n in rows.all()}
    spaces: list[SpaceInfo] = []
    for view in shown:
        can_edit = await documents_core.caller_has_space_role(
            session,
            user_id=ext.user.id,
            organization_id=ext.organization.id,
            space_id=view.space.id,
            required="editor",
        )
        spaces.append(
            SpaceInfo(
                name=view.space.name,
                kind="personal" if view.space.preset == "personal" and view.is_owner else "shared",
                can_edit=can_edit,
                document_count=counts.get(view.space.id, 0),
                shared_only_documents=view.shared_only_documents,
            )
        )
    await _audit_listing(session, ext, what="spaces", result_count=len(spaces))
    return SpacesResponse(organization=ext.organization.name, spaces=spaces, total=len(views))


@router.get(
    "/documents",
    response_model=DocumentsResponse,
    summary="List the documents in one space",
)
async def list_documents(
    space: str = Query(min_length=1, max_length=200),
    cursor: Optional[str] = Query(default=None, max_length=500),
    limit: int = Query(default=_MAX_DOCUMENTS_PER_PAGE, ge=1, le=_MAX_DOCUMENTS_PER_PAGE),
    type: Optional[str] = Query(
        default=None,
        pattern="^(image|pdf|word|spreadsheet|presentation|text|other)$",
        description="Only documents of this kind.",
    ),
    ext: core.ExternalCaller = Depends(require_external_caller),
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> DocumentsResponse:
    _require_read(ext)
    await _list_rate_limit(infra, ext)
    after = _decode_cursor(cursor) if cursor else None

    try:
        view = await spaces_service.resolve_target_space(
            session,
            organization_id=ext.organization.id,
            user_id=ext.user.id,
            name=space,
            include_shared=True,
        )
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "space_not_found", "message": str(exc)},
        ) from exc
    if view.space.status != "active":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "space_not_found", "message": "no accessible space with that name"},
        )

    access = await load_doc_access(
        session, user_id=ext.user.id, organization_id=ext.organization.id
    )
    base = (
        Document.organization_id == ext.organization.id,
        Document.space_id == view.space.id,
        Document.status != "archived",
        # Approved files, and the caller's own that are waiting or refused (so they can see why
        # a file they uploaded is not searchable). Nobody else's.
        or_(Document.review_state == "approved", Document.uploaded_by_user_id == ext.user.id),
        visibility_clause(access, whole_space=view.shared_only_documents == 0),
    )
    if type:
        base = (*base, documents_core.file_type_clause(Document.mime_type, type))
    count_stmt = select(func.count()).select_from(Document).where(*base)
    total = int((await session.execute(count_stmt)).scalar_one())
    stmt = select(
        Document.id,
        Document.name,
        Document.mime_type,
        Document.size_bytes,
        Document.updated_at,
        Document.indexing_state,
        Document.review_state,
        Document.review_note,
    ).where(*base)
    if after is not None:
        when, last_id = after
        stmt = stmt.where(
            or_(
                Document.updated_at < when,
                and_(Document.updated_at == when, Document.id < last_id),
            )
        )
    rows = (
        await session.execute(
            stmt.order_by(Document.updated_at.desc(), Document.id.desc()).limit(limit + 1)
        )
    ).all()
    page, more = rows[:limit], len(rows) > limit
    now = datetime.now(timezone.utc)
    documents = [
        DocumentInfo(
            name=name,
            mime_type=mime,
            size_bytes=int(size),
            updated_at=updated,
            status=_public_status(state, updated, now, review_state),
            review_note=(note or None) if review_state == "rejected" else None,
        )
        for _id, name, mime, size, updated, state, review_state, note in page
    ]
    next_cursor = _encode_cursor(page[-1][4], page[-1][0]) if more and page else None
    await _audit_listing(
        session, ext, what="documents", space_id=str(view.space.id), result_count=len(documents)
    )
    return DocumentsResponse(
        space_name=view.space.name,
        documents=documents,
        total=total,
        next_cursor=next_cursor,
        type=type or None,
    )


# ---------------------------------------------------------------------------
# Picture search with previews (requires knowledge:read)
# ---------------------------------------------------------------------------
#
# Finds pictures by a description, in the spaces the person can read now. Each
# result carries a short-lived preview link (see ``services.external_image_links``)
# because an outside tool cannot show a picture behind a bearer token. No picture
# bytes are returned here, and nothing the person types is kept.

_IMAGE_SEARCH_RATE_LIMIT = 30  # per grant per minute: each search runs the picture model
_PREVIEW_RATE_LIMIT = 240  # per grant per minute: one search can show up to 10 previews
_MAX_IMAGE_RESULTS = 10


class ImageSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=image_search.MAX_QUERY_TEXT_CHARS)
    top_k: int = Field(default=6, ge=1, le=_MAX_IMAGE_RESULTS)
    # Name of one space to look in; omit to look in every space the person can read.
    space: Optional[str] = Field(default=None, max_length=200)


class ImageSearchResponse(BaseModel):
    organization: str
    images: list[ImageResult]
    searched_spaces: int
    preview_expires_in: int


def _public_origin(request: Request) -> str:
    configured = get_settings().public_base_url
    return (configured or str(request.base_url)).rstrip("/")


@router.post(
    "/images/search",
    response_model=ImageSearchResponse,
    summary="Find pictures by a description",
)
async def search_images_by_description(
    payload: ImageSearchRequest,
    request: Request,
    ext: core.ExternalCaller = Depends(require_external_caller),
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> ImageSearchResponse:
    _require_read(ext)
    try:
        await _image_rate_limit(infra, ext)
    except core.OAuthError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail={"code": exc.error, "message": exc.description},
            headers={"Retry-After": "60"} if exc.status == 429 else None,
        ) from exc

    requested = await _requested_spaces(session, ext, payload.space)
    scope = await image_search.resolve_image_access(
        session,
        organization_id=ext.organization.id,
        caller_user_id=ext.user.id,
        requested_space_ids=requested,
    )
    try:
        vector = await image_search.embed_query(image=None, text=payload.query)
        found = await image_search.search_images(
            session,
            qdrant=infra.qdrant,
            organization_id=ext.organization.id,
            space_ids=scope.space_ids,
            query_vector=vector,
            top_k=payload.top_k,
            doc_access=scope.doc_access,
        )
    except image_search.ImageSearchDisabled as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "image_search_disabled", "message": str(exc)},
        ) from exc
    except image_search.ImageSearchError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "bad_query", "message": str(exc)},
        ) from exc

    images = [_image_result(request, ext, m) for m in found.matches]
    organization_name = ext.organization.name
    document_ids = [str(m.document_id) for m in found.matches]
    await session.commit()  # end the read transaction before the audit write
    async with session.begin():
        session.add(
            AuditEvent(
                organization_id=ext.organization.id,
                actor_type="user",
                actor_id=ext.user.id,
                action="knowledge.external_image_search",
                resource_type="oauth_grant",
                resource_id=ext.grant_id,
                payload={
                    "grant_id": str(ext.grant_id),
                    "client_name": ext.client_name,
                    "result_count": len(images),
                    # Ids only; the description is not recorded.
                    "retrieved_document_ids": document_ids,
                },
            )
        )
    return ImageSearchResponse(
        organization=organization_name,
        images=images,
        searched_spaces=len(found.space_ids),
        preview_expires_in=external_image_links.LINK_TTL_SECONDS,
    )


def _gone() -> HTTPException:
    # One answer for every reason (bad link, expired, revoked, no access, deleted):
    # the link tells a stranger nothing about which.
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")


@router.get(
    "/preview/{token}.jpg",
    summary="A small JPEG preview named by a short-lived link",
    responses={200: {"content": {"image/jpeg": {}}}},
)
async def preview_by_link(
    token: str,
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> Response:
    """No bearer header: the link is the credential, and it is re-checked on every use.

    Fails closed with a plain 404 unless the link is genuine and unexpired **and**
    the grant behind it is still live, the person still has the permission to read
    the space and the picture, and the picture still exists and is not archived.
    """

    link = external_image_links.verify(get_settings(), token)
    if link is None:
        raise _gone()
    try:
        await core.rate_limit(
            infra.redis,
            key=f"quberi:external:preview:{link.grant_id}",
            limit=_PREVIEW_RATE_LIMIT,
            window_seconds=60,
        )
    except core.OAuthError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail="too many requests",
            headers={"Retry-After": "60"} if exc.status == 429 else None,
        ) from exc

    now = datetime.now(timezone.utc)
    row = (
        await session.execute(
            select(OAuthGrant, User, Organization)
            .join(User, User.id == OAuthGrant.user_id)
            .join(Organization, Organization.id == OAuthGrant.organization_id)
            .where(OAuthGrant.id == link.grant_id, OAuthGrant.user_id == link.user_id)
        )
    ).first()
    if row is None:
        raise _gone()
    grant, user, org = row
    if (
        grant.revoked_at is not None
        or grant.absolute_expires_at <= now
        or user.status != "active"
        or org.status != "active"
        or not org.allow_external_tools
        or clients.SCOPE_KNOWLEDGE_READ not in grant.scope.split()
    ):
        raise _gone()

    doc = (
        await session.execute(
            select(Document, Space.status)
            .join(Space, Space.id == Document.space_id)
            .where(
                Document.id == link.document_id,
                Document.organization_id == org.id,
                Space.organization_id == org.id,
            )
        )
    ).first()
    if doc is None:
        raise _gone()
    document, space_status = doc
    if document.status == "archived" or space_status != "active" or not is_picture(document.mime_type):
        raise _gone()
    try:
        # The same permission check as a signed-in download of this document.
        document, stream = await documents_core.open_download_stream(
            session,
            document_id=document.id,
            organization_id=org.id,
            space_id=document.space_id,
            caller_user_id=user.id,
        )
        data = await render_thumbnail(stream)
    except (NotFoundError, DocumentPermissionError):
        raise _gone() from None
    except ThumbnailError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'",
        },
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
    # 'approved', or 'pending' when the space asks for approval of uploads: the note is saved
    # but not indexed (so not searchable) until an owner or admin of the space approves it.
    review_state: str = "approved"
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
        review_state=view.document.review_state,
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
    # 'approved', or 'pending' when the space asks for approval of uploads (see SaveResponse).
    review_state: str = "approved"
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
        review_state=view.document.review_state,
    )


external_router = router
