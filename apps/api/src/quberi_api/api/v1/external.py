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

Every route here is a thin HTTP adapter: it parses the request, calls one of
the ``do_xxx`` functions in ``quberi_api.services.mcp.adapters``, and turns the
result (or an ``AdapterHTTPError``) into an HTTP response. The actual business
logic — permission checks, rate limiting, the service-layer calls, and audit
events — lives in ``adapters.py`` so the MCP ``tools/call`` dispatcher
(``api/v1/mcp/router.py``) can call the exact same functions and get
byte-for-byte the same behavior. This split exists specifically so retrieval
and permission logic is never implemented twice (see the brief's "do not
reimplement retrieval/permission logic" rule and design.md §2.5).

A handful of names below (the rate-limit constants, ``_read_capped``,
``_encode_cursor``, ``retrieve_for_user``) are re-exported from
``adapters``/``rag.retrieve`` rather than being new definitions, purely so
existing tests that do ``monkeypatch.setattr(external_api, "_NAME", ...)`` or
``from quberi_api.api.v1.external import _pending_documents`` keep working
unchanged — patching the re-exported name here also patches what the adapter
functions see, because they're the same object.
"""

from __future__ import annotations

import uuid
from typing import Optional

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session, get_infrastructure
from quberi_api.api.v1.external_schemas import (
    DocumentsResponse,
    ImageSearchRequest,
    ImageSearchResponse,
    ReadAroundRequest,
    ReadAroundResponse,
    SaveRequest,
    SaveResponse,
    SearchRequest,
    SearchResponse,
    SpacesResponse,
    UploadResponse,
)
from quberi_api.core.config import get_settings
from quberi_api.domain.documents.models import Document
from quberi_api.domain.identity.models import Organization, User
from quberi_api.domain.oauth.models import OAuthGrant
from quberi_api.domain.spaces.models import Space
from quberi_api.infrastructure.registry import Infrastructure
from quberi_api.services import external_image_links, external_page_links
from quberi_api.services.documents import core as documents_core
from quberi_api.services.documents import new_version as new_version_service
from quberi_api.services.documents import page_render
from quberi_api.services.documents.errors import DocumentPermissionError
from quberi_api.services.documents.thumbnail import ThumbnailError, render_thumbnail
from quberi_api.services.errors import NotFoundError
from quberi_api.services.mcp import adapters
from quberi_api.services.oauth import clients, core
from quberi_api.services.rag.indexer import is_picture
from quberi_api.services.rag.retrieve import (
    retrieve_for_user,  # noqa: F401 - re-exported, see module docstring
)

router = APIRouter(prefix="/external", tags=["external"])

_bearer = HTTPBearer(auto_error=False)

# Re-exported so ``monkeypatch.setattr(external_api, "_NAME", ...)`` in existing
# tests still patches the value the adapter functions read (see module docstring).
_SEARCH_RATE_LIMIT = adapters.SEARCH_RATE_LIMIT
_SAVE_RATE_LIMIT = adapters.SAVE_RATE_LIMIT
_UPLOAD_RATE_LIMIT = adapters.UPLOAD_RATE_LIMIT
_IMAGE_SEARCH_RATE_LIMIT = adapters.IMAGE_SEARCH_RATE_LIMIT
_PREVIEW_RATE_LIMIT = adapters.PREVIEW_RATE_LIMIT
_PAGE_RATE_LIMIT = adapters.PAGE_RATE_LIMIT
_LIST_RATE_LIMIT = adapters.LIST_RATE_LIMIT
_MAX_SPACES_LISTED = adapters.MAX_SPACES_LISTED
_MAX_DOCUMENTS_PER_PAGE = adapters.MAX_DOCUMENTS_PER_PAGE


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


def _raise(exc: adapters.AdapterHTTPError) -> None:
    raise HTTPException(
        status_code=exc.status_code, detail=exc.detail, headers=exc.headers
    ) from exc


@router.post("/search", response_model=SearchResponse, summary="Search readable knowledge")
async def search(
    payload: SearchRequest,
    request: Request,
    ext: core.ExternalCaller = Depends(require_external_caller),
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> SearchResponse:
    try:
        return await adapters.do_search(payload, request, ext, session, infra)
    except adapters.AdapterHTTPError as exc:
        _raise(exc)


@router.post(
    "/read-around",
    response_model=ReadAroundResponse,
    summary="Read the text on either side of a passage a search returned",
)
async def read_around(
    payload: ReadAroundRequest,
    ext: core.ExternalCaller = Depends(require_external_caller),
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> ReadAroundResponse:
    try:
        return await adapters.do_read_around(payload, ext, session, infra)
    except adapters.AdapterHTTPError as exc:
        _raise(exc)


@router.post(
    "/images/search", response_model=ImageSearchResponse, summary="Find pictures by a description"
)
async def search_images_by_description(
    payload: ImageSearchRequest,
    request: Request,
    ext: core.ExternalCaller = Depends(require_external_caller),
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> ImageSearchResponse:
    try:
        return await adapters.do_search_images(payload, request, ext, session, infra)
    except adapters.AdapterHTTPError as exc:
        _raise(exc)


def _gone() -> HTTPException:
    # One answer for every reason (bad link, expired, revoked, no access, deleted):
    # the link tells a stranger nothing about which.
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")


async def _live_grant(
    session: AsyncSession, *, grant_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[User, Organization]:
    """The person and organisation behind a signed link, if the grant is still good.

    Re-checked on every use of a link, never trusted from when it was signed: the tool may
    have been revoked, the person deactivated, the organisation may have switched outside
    tools off, or the grant may have lost the permission to read.
    """

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    row = (
        await session.execute(
            select(OAuthGrant, User, Organization)
            .join(User, User.id == OAuthGrant.user_id)
            .join(Organization, Organization.id == OAuthGrant.organization_id)
            .where(OAuthGrant.id == grant_id, OAuthGrant.user_id == user_id)
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
    return user, org


_NO_STORE = {
    "Cache-Control": "private, max-age=300",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'",
}


@router.get(
    "/page/{token}.jpg",
    summary="An image of one page of a document, named by a short-lived link",
    responses={200: {"content": {"image/jpeg": {}}}},
)
async def page_by_link(
    token: str,
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> Response:
    """No bearer header: the link is the credential, and it is re-checked on every use.

    Like the picture preview, it fails closed with one plain 404 for every reason: a bad or
    expired link, a revoked tool, a person who lost access, a document or version that is gone,
    archived, hidden or not yet approved. For a PDF it is the page the link names; for a picture
    it is the picture itself, larger than its search preview.
    """

    link = external_page_links.verify(get_settings(), token)
    if link is None:
        raise _gone()
    try:
        await core.rate_limit(
            infra.redis,
            key=f"quberi:external:page:{link.grant_id}",
            limit=_PAGE_RATE_LIMIT,
            window_seconds=60,
        )
    except core.OAuthError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail="too many requests",
            headers={"Retry-After": "60"} if exc.status == 429 else None,
        ) from exc

    user, org = await _live_grant(session, grant_id=link.grant_id, user_id=link.user_id)
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
    if document.status == "archived" or space_status != "active":
        raise _gone()
    legacy = document.current_version_id is None and link.version_number == 1
    try:
        try:
            # The same permission check as a signed-in download of this version.
            version, stream = await new_version_service.open_version_stream(
                session,
                document_id=document.id,
                version_number=link.version_number,
                organization_id=org.id,
                space_id=document.space_id,
                caller_user_id=user.id,
            )
            mime, filename = (version.mime_type or "").lower(), version.filename
        except NotFoundError:
            if not legacy:
                raise
            # A file stored before versions were recorded has no version row: its one
            # version is the file itself, opened with the same check as a download.
            opened, stream = await documents_core.open_download_stream(
                session,
                document_id=document.id,
                organization_id=org.id,
                space_id=document.space_id,
                caller_user_id=user.id,
            )
            mime, filename = (opened.mime_type or "").lower(), opened.name
        if mime == "application/pdf" or filename.lower().endswith(".pdf"):
            data = await page_render.render_pdf_page(stream, page=link.page)
        elif is_picture(mime):
            data = await page_render.render_picture(stream)
        else:
            raise _gone()
    except (NotFoundError, DocumentPermissionError):
        raise _gone() from None
    except ThumbnailError as exc:
        # A page that does not exist is the same "not found" as everything else.
        if exc.status == 404:
            raise _gone() from None
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return Response(content=data, media_type="image/jpeg", headers=_NO_STORE)


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

    This one endpoint was not moved into ``adapters.py``: it authenticates via the
    signed link itself (not a bearer token / ``ExternalCaller``), so it does not
    fit the ``do_xxx(ext, ...)`` shape the other six share, and the MCP tools do
    not call it directly — they return this same URL in their output for a
    client's own HTTP GET to follow (design.md §2.8).
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

    user, org = await _live_grant(session, grant_id=link.grant_id, user_id=link.user_id)

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
    is_archived = document.status == "archived"
    if is_archived or space_status != "active" or not is_picture(document.mime_type):
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
    try:
        return await adapters.do_save_note(payload, ext, session, infra)
    except adapters.AdapterHTTPError as exc:
        _raise(exc)


@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file into readable knowledge",
)
async def upload(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    space: Optional[str] = Form(default=None, max_length=200),
    ext: core.ExternalCaller = Depends(require_external_caller),
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> UploadResponse:
    try:
        result = await adapters.do_upload(request, file, space, ext, session, infra)
    except adapters.AdapterHTTPError as exc:
        _raise(exc)
    if result.duplicate:
        # Nothing was created, so not 201.
        response.status_code = status.HTTP_200_OK
    return result


@router.get("/spaces", response_model=SpacesResponse, summary="List the spaces the person can read")
async def list_spaces(
    ext: core.ExternalCaller = Depends(require_external_caller),
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> SpacesResponse:
    try:
        return await adapters.do_list_spaces(ext, session, infra)
    except adapters.AdapterHTTPError as exc:
        _raise(exc)


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
    try:
        return await adapters.do_list_documents(
            ext, session, infra, space=space, cursor=cursor, limit=limit, type=type
        )
    except adapters.AdapterHTTPError as exc:
        _raise(exc)


# ---------------------------------------------------------------------------
# Re-exports kept for existing tests that import these private names directly
# from this module (``from quberi_api.api.v1.external import _pending_documents``,
# ``external_api._read_capped``, ``external_api._encode_cursor``). They are the
# exact same functions ``adapters.py`` uses, not copies — see module docstring.
# ---------------------------------------------------------------------------

_pending_documents = adapters.pending_documents
_read_capped = adapters.read_capped
_encode_cursor = adapters.encode_cursor
_decode_cursor = adapters.decode_cursor
_resolve_space = adapters.resolve_space_or_error
_requested_spaces = adapters.requested_spaces
_public_origin = adapters.public_origin
_image_result = adapters.image_result
_related_pictures = adapters.related_pictures
_public_status = adapters.public_status
_audit_listing = adapters.audit_listing
_require_read = adapters.require_read
_image_rate_limit = adapters.image_rate_limit
_list_rate_limit = adapters.list_rate_limit
_too_large = adapters.too_large
_gone_ = _gone  # avoid an unused-import lint false positive on the local helper

external_router = router
