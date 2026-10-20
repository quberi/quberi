"""Business logic shared by the REST external API and the MCP tool handlers.

Each ``do_xxx`` function here is the *entire* business logic of one of the six
knowledge capabilities exposed to outside tools (search, search_images,
list_spaces, list_documents, save_note, upload). It used to live directly
inside the route handler functions in ``api/v1/external.py``; it was moved
here unchanged so that both the REST router (a thin wrapper that adapts an
HTTP request/response) and the MCP ``tools/call`` dispatcher (a thin wrapper
that adapts a JSON-RPC request/response) call the exact same code path for
permission checks, rate limiting, the service-layer calls, and audit logging.

This is the concrete implementation of the design's "do not reimplement
retrieval/permission logic" rule (brief + design.md §2.5): MCP does not call
``retrieve_for_user`` on its own and reimplement the surrounding scope/rate
limit/audit logic — it calls the exact function the REST path also calls.

Nothing about behavior changed during this extraction: every exception type,
every rate-limit key, every audit event shape is identical to what
``external.py`` did before. The REST route handlers in ``external.py`` still
raise ``HTTPException`` with the same status codes and ``detail`` shapes as
before — they just get there by calling one of the ``do_xxx`` functions below
instead of containing the logic directly. See ``api/v1/external.py``'s module
docstring for the REST side, and ``services/mcp/formatting.py`` for how the
MCP side turns these same return values into tool-call content.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Request, UploadFile
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.v1.external_schemas import (
    DocumentInfo,
    DocumentsResponse,
    ImageResult,
    ImageSearchRequest,
    ImageSearchResponse,
    Passage,
    PendingDocument,
    SaveRequest,
    SaveResponse,
    SearchRequest,
    SearchResponse,
    SpaceInfo,
    SpacesResponse,
    UploadResponse,
)
from quberi_api.core.config import get_settings
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document
from quberi_api.domain.spaces.models import Space
from quberi_api.infrastructure.registry import Infrastructure
from quberi_api.services import documents as documents_service
from quberi_api.services import external_image_links
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
from quberi_api.services.rag import image_search
from quberi_api.services.rag.retrieve import retrieve_for_user
from quberi_api.services.retrieval_audit import document_ids_from_chunks

# ---------------------------------------------------------------------------
# Shared constants (unchanged from external.py; several are patched by name
# in existing tests via ``monkeypatch.setattr(external_api, "_NAME", ...)``,
# so external.py re-exports these same objects rather than defining its own
# copies — see that module for the re-export list).
# ---------------------------------------------------------------------------

SEARCH_RATE_LIMIT = 60  # per grant per minute
SAVE_RATE_LIMIT = 30  # per grant per minute — writes are rarer than searches
UPLOAD_RATE_LIMIT = 10  # per grant per minute — files are heavier than notes
IMAGE_SEARCH_RATE_LIMIT = 30  # per grant per minute: each search runs the picture model
PREVIEW_RATE_LIMIT = 240  # per grant per minute: one search can show up to 10 previews
MAX_IMAGE_RESULTS = 10
MAX_PASSAGE_CHARS = 4_000
MAX_PENDING_LISTED = 10
PENDING_WINDOW = timedelta(hours=2)
PENDING_STATES = ("not_indexed", "indexing")
LIST_RATE_LIMIT = 60  # per grant per minute, shared by both listing endpoints
MAX_SPACES_LISTED = 100
MAX_DOCUMENTS_PER_PAGE = 50
MAX_SPACE_CHOICES = 20


class AdapterHTTPError(Exception):
    """A business error, carrying exactly what the REST layer needs to answer.

    The REST route handlers catch this and raise ``HTTPException(status_code,
    detail, headers)`` with these three fields verbatim — this preserves the
    exact REST response shape from before the extraction. The MCP router
    catches the same exception and maps it to a tool-execution error via
    ``services/mcp/formatting.format_error`` (design.md §2.7); MCP's mapping
    uses ``code``/``message`` from ``detail`` and does not use ``headers``
    (there are no custom HTTP headers in a JSON-RPC tool-call response body).
    """

    def __init__(self, status_code: int, detail: object, headers: Optional[dict] = None) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail
        self.headers = headers


def too_large(limit: int) -> AdapterHTTPError:
    return AdapterHTTPError(
        413,
        {"code": "file_too_large", "message": f"the file is larger than the {limit}-byte limit"},
    )


def require_scope(ext: core.ExternalCaller, scope: str) -> None:
    if not ext.has_scope(scope):
        raise AdapterHTTPError(
            403, {"code": "insufficient_scope", "message": f"{scope} is required"}
        )


async def rate_limited(infra: Infrastructure, *, key: str, limit: int) -> None:
    try:
        await core.rate_limit(infra.redis, key=key, limit=limit, window_seconds=60)
    except core.OAuthError as exc:
        raise AdapterHTTPError(
            exc.status,
            {"code": exc.error, "message": exc.description},
            headers={"Retry-After": "60"} if exc.status == 429 else None,
        ) from exc


async def resolve_space_or_error(
    session: AsyncSession, ext: core.ExternalCaller, name: Optional[str]
) -> spaces_service.SpaceView:
    """Pick the space a save or upload goes to, or raise a clear error.

    With no name, the target is the person's personal space. If they have
    none, say so (409 ``space_required``) and list the spaces they can reach so
    the tool can ask which one — never drop the content into a shared space
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
        raise AdapterHTTPError(
            409,
            {
                "code": "space_required",
                "message": "You have no personal space; say which space to use.",
                "spaces": exc.candidates[:MAX_SPACE_CHOICES],
            },
        ) from exc
    except NotFoundError as exc:
        raise AdapterHTTPError(
            404, {"code": "space_not_found", "message": str(exc)}
        ) from exc


async def requested_spaces(
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
        raise AdapterHTTPError(
            404, {"code": "space_not_found", "message": str(exc)}
        ) from exc
    return [view.space.id]


def public_origin(request: Request) -> str:
    configured = get_settings().public_base_url
    return (configured or str(request.base_url)).rstrip("/")


def preview_link(
    request: Request, ext: core.ExternalCaller, document_id: uuid.UUID
) -> Optional[str]:
    settings = get_settings()
    if not external_image_links.links_available(settings):
        return None
    token = external_image_links.sign(
        settings, user_id=ext.user.id, grant_id=ext.grant_id, document_id=document_id
    )
    return f"{public_origin(request)}/api/v1/external/preview/{token}.jpg"


def image_result(
    request: Request, ext: core.ExternalCaller, m: image_search.ImageMatch
) -> ImageResult:
    return ImageResult(
        name=m.document_name,
        space_name=m.space_name,
        mime_type=m.mime_type,
        size_bytes=m.size_bytes,
        score=round(m.score, 4),
        preview_url=preview_link(request, ext, m.document_id),
    )


async def image_rate_limit(infra: Infrastructure, ext: core.ExternalCaller) -> None:
    """Each picture lookup runs the picture model, so it has a limit of its own."""

    await rate_limited(
        infra, key=f"quberi:external:imagesearch:{ext.grant_id}", limit=IMAGE_SEARCH_RATE_LIMIT
    )


# ---------------------------------------------------------------------------
# do_search_images — POST /images/search, and the ``kind=image`` branch of search
# ---------------------------------------------------------------------------


async def do_search_images(
    payload: ImageSearchRequest,
    request: Request,
    ext: core.ExternalCaller,
    session: AsyncSession,
    infra: Infrastructure,
) -> ImageSearchResponse:
    require_scope(ext, clients.SCOPE_KNOWLEDGE_READ)
    await image_rate_limit(infra, ext)

    requested = await requested_spaces(session, ext, payload.space)
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
        raise AdapterHTTPError(
            503, {"code": "image_search_disabled", "message": str(exc)}
        ) from exc
    except image_search.ImageSearchError as exc:
        raise AdapterHTTPError(400, {"code": "bad_query", "message": str(exc)}) from exc

    images = [image_result(request, ext, m) for m in found.matches]
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


# ---------------------------------------------------------------------------
# do_search — POST /search (text, plus the related-pictures side lookup)
# ---------------------------------------------------------------------------


async def related_pictures(
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
        await image_rate_limit(infra, ext)
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
    except Exception:  # noqa: BLE001 - a side lookup must never break the search (including
        # a rate-limit AdapterHTTPError: swallowing it here, not just generic errors, is the
        # original behaviour this function was extracted from, unchanged)
        import logging

        logging.getLogger(__name__).warning("related pictures failed", exc_info=True)
        return []
    return [
        (m, image_result(request, ext, m))
        for m in found.matches
        if m.score >= RELATED_PICTURE_MIN_SCORE
    ]


async def pending_documents(
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
    cutoff = datetime.now(timezone.utc) - PENDING_WINDOW
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
                Document.indexing_state.in_(PENDING_STATES),
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
        .limit(MAX_PENDING_LISTED)
    )
    return int(total), [
        (name, sid, "awaiting_approval" if review == "pending" else state)
        for name, sid, state, review in rows.all()
    ]


async def do_search(
    payload: SearchRequest,
    request: Request,
    ext: core.ExternalCaller,
    session: AsyncSession,
    infra: Infrastructure,
) -> SearchResponse:
    require_scope(ext, clients.SCOPE_KNOWLEDGE_READ)
    if payload.kind == "image":
        # Pictures only: the same lookup, limits and audit as the dedicated endpoint.
        if len(payload.query) > image_search.MAX_QUERY_TEXT_CHARS:
            raise AdapterHTTPError(
                400,
                {
                    "code": "bad_query",
                    "message": (
                        f"describe the picture in at most "
                        f"{image_search.MAX_QUERY_TEXT_CHARS} characters"
                    ),
                },
            )
        pictures = await do_search_images(
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

    await rate_limited(infra, key=f"quberi:external:search:{ext.grant_id}", limit=SEARCH_RATE_LIMIT)

    requested = await requested_spaces(session, ext, payload.space)
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
    pending_total, pending_rows = await pending_documents(
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
        await related_pictures(
            request, session, infra, ext, query=payload.query, requested=requested
        )
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
                text=c.text[:MAX_PASSAGE_CHARS],
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


# ---------------------------------------------------------------------------
# Browse: which spaces, and which documents in one (requires knowledge:read)
# ---------------------------------------------------------------------------
#
# Metadata only. Neither endpoint returns any document text (search is the one
# path for that) or who uploaded what. Both read through the same live access
# rules as search: a space or document the person cannot see in Quberi is not
# listed here.


def require_read(ext: core.ExternalCaller) -> None:
    require_scope(ext, clients.SCOPE_KNOWLEDGE_READ)


async def list_rate_limit(infra: Infrastructure, ext: core.ExternalCaller) -> None:
    await rate_limited(infra, key=f"quberi:external:list:{ext.grant_id}", limit=LIST_RATE_LIMIT)


def public_status(
    state: str, updated_at: datetime, now: datetime, review_state: str = "approved"
) -> str:
    # A file waiting for (or refused) approval is not "being indexed": say what it is waiting for.
    if review_state == "pending":
        return "awaiting_approval"
    if review_state == "rejected":
        return "rejected"
    if state == "indexed":
        return "ready"
    if state in PENDING_STATES and now - updated_at <= PENDING_WINDOW:
        return "indexing"
    return "not_searchable"


async def audit_listing(session: AsyncSession, ext: core.ExternalCaller, **payload: object) -> None:
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


async def do_list_spaces(
    ext: core.ExternalCaller, session: AsyncSession, infra: Infrastructure
) -> SpacesResponse:
    require_read(ext)
    await list_rate_limit(infra, ext)

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
    shown = views[:MAX_SPACES_LISTED]
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
    await audit_listing(session, ext, what="spaces", result_count=len(spaces))
    return SpacesResponse(organization=ext.organization.name, spaces=spaces, total=len(views))


async def do_list_documents(
    ext: core.ExternalCaller,
    session: AsyncSession,
    infra: Infrastructure,
    *,
    space: str,
    cursor: Optional[str] = None,
    limit: int = MAX_DOCUMENTS_PER_PAGE,
    type: Optional[str] = None,  # noqa: A002 - matches the REST query param name
) -> DocumentsResponse:
    require_read(ext)
    await list_rate_limit(infra, ext)
    after = decode_cursor(cursor) if cursor else None

    try:
        view = await spaces_service.resolve_target_space(
            session,
            organization_id=ext.organization.id,
            user_id=ext.user.id,
            name=space,
            include_shared=True,
        )
    except NotFoundError as exc:
        raise AdapterHTTPError(404, {"code": "space_not_found", "message": str(exc)}) from exc
    if view.space.status != "active":
        raise AdapterHTTPError(
            404, {"code": "space_not_found", "message": "no accessible space with that name"}
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
            status=public_status(state, updated, now, review_state),
            review_note=(note or None) if review_state == "rejected" else None,
        )
        for _id, name, mime, size, updated, state, review_state, note in page
    ]
    next_cursor = encode_cursor(page[-1][4], page[-1][0]) if more and page else None
    await audit_listing(
        session, ext, what="documents", space_id=str(view.space.id), result_count=len(documents)
    )
    return DocumentsResponse(
        space_name=view.space.name,
        documents=documents,
        total=total,
        next_cursor=next_cursor,
        type=type or None,
    )


def encode_cursor(updated_at: datetime, doc_id: uuid.UUID) -> str:
    import base64
    import json

    raw = json.dumps({"t": updated_at.isoformat(), "i": str(doc_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    import base64
    import binascii
    import json

    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        when = datetime.fromisoformat(data["t"])
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when, uuid.UUID(data["i"])
    except (ValueError, KeyError, TypeError, binascii.Error, json.JSONDecodeError) as exc:
        raise AdapterHTTPError(
            400, {"code": "invalid_cursor", "message": "the page cursor is not valid"}
        ) from exc


# ---------------------------------------------------------------------------
# do_save_note — POST /save (requires knowledge:write)
# ---------------------------------------------------------------------------


async def do_save_note(
    payload: SaveRequest, ext: core.ExternalCaller, session: AsyncSession, infra: Infrastructure
) -> SaveResponse:
    require_scope(ext, clients.SCOPE_KNOWLEDGE_WRITE)
    await rate_limited(infra, key=f"quberi:external:save:{ext.grant_id}", limit=SAVE_RATE_LIMIT)

    # Resolve the target space from a human-friendly name (or the personal
    # default). The write permission itself is re-checked inside save_note.
    target = await resolve_space_or_error(session, ext, payload.space)
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
        raise AdapterHTTPError(403, {"code": "forbidden", "message": str(exc)}) from exc
    except (DocumentValidationError, UnsupportedMimeError) as exc:
        raise AdapterHTTPError(400, {"code": "invalid_note", "message": str(exc)}) from exc

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
# do_upload — POST /upload (requires knowledge:upload)
# ---------------------------------------------------------------------------


async def read_capped(upload: UploadFile, limit: int) -> bytes:
    """Read the file in pieces and stop as soon as it passes ``limit``."""

    buf = bytearray()
    while True:
        piece = await upload.read(1024 * 1024)
        if not piece:
            break
        buf.extend(piece)
        if len(buf) > limit:
            raise too_large(limit)
    return bytes(buf)


async def do_upload_bytes(
    *,
    data: bytes,
    filename: str,
    declared_mime: str,
    space: Optional[str],
    ext: core.ExternalCaller,
    session: AsyncSession,
) -> UploadResponse:
    """The part of ``upload`` after the bytes are already in memory.

    Split out from the REST route's multipart/``_read_capped`` reading so the
    MCP tool (whose input is a base64 string decoded by the router, not a
    multipart stream) can share this exact tail — type sniffing, space
    resolution, the ``upload_local`` call, and the audit event — with the
    REST path. See ``api/v1/mcp/router.py`` for the base64-decoding call site
    and the size pre-check that runs before this function is ever reached.
    """

    try:
        checked = external_upload.check_upload(
            data=data, filename=filename, declared_mime=declared_mime
        )
    except UnsupportedMimeError as exc:
        raise AdapterHTTPError(415, {"code": "unsupported_type", "message": str(exc)}) from exc
    except DocumentValidationError as exc:
        raise AdapterHTTPError(400, {"code": "invalid_file", "message": str(exc)}) from exc

    target = await resolve_space_or_error(session, ext, space)
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
        raise AdapterHTTPError(403, {"code": "forbidden", "message": str(exc)}) from exc
    except UnsupportedMimeError as exc:
        raise AdapterHTTPError(415, {"code": "unsupported_type", "message": str(exc)}) from exc
    except DocumentValidationError as exc:
        status_code = 413 if "limit" in str(exc) else 400
        raise AdapterHTTPError(status_code, {"code": "invalid_file", "message": str(exc)}) from exc

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


async def do_upload(
    request: Request,
    file: UploadFile,
    space: Optional[str],
    ext: core.ExternalCaller,
    session: AsyncSession,
    infra: Infrastructure,
) -> UploadResponse:
    # Scope first, before touching the body at all.
    require_scope(ext, clients.SCOPE_KNOWLEDGE_UPLOAD)
    await rate_limited(infra, key=f"quberi:external:upload:{ext.grant_id}", limit=UPLOAD_RATE_LIMIT)

    limit = documents_service.MAX_UPLOAD_BYTES
    # Refuse an honest oversized request without reading it; a dishonest one is
    # caught by the capped read below.
    declared_length = request.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > limit + 64 * 1024:
        raise too_large(limit)

    data = await read_capped(file, limit)
    return await do_upload_bytes(
        data=data,
        filename=file.filename or "",
        declared_mime=file.content_type or "",
        space=space,
        ext=ext,
        session=session,
    )
