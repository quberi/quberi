"""Document CRUD + download surface.

Authorisation rules
-------------------
Documents inherit access from their parent space:

* ``list_documents_in_space``  — viewer / editor / admin / owner
* ``get_document`` / ``open_download_stream`` — same
* ``upload_local`` — editor / admin / owner
* ``delete_document`` — admin / owner OR the uploader themselves

We call the Slice-B evaluator (``resolve_effective_role_codes``) so a
caller who has access through a group / position / org-unit binding is
treated identically to a directly-bound user.
"""

from __future__ import annotations

import io
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, BinaryIO, Optional

from sqlalchemy import and_, case, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document
from quberi_api.domain.identity.models import User
from quberi_api.domain.spaces.models import Space
from quberi_api.services import access_guard, doc_acl, document_sources
from quberi_api.services.authz import resolve_effective_role_codes
from quberi_api.services.documents.connectors import local as local_connector
from quberi_api.services.documents.errors import (
    DocumentPermissionError,
    DocumentValidationError,
    UnsupportedMimeError,
)
from quberi_api.services.errors import NotFoundError
from quberi_api.workers.enqueue import enqueue_index_document

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MiB
MAX_FILENAME_LENGTH = 200

# Allow-list keeps the immediate blast radius small. When we start
# accepting archive formats or code drops we can extend this via
# ``settings`` rather than editing here.
_ALLOWED_MIME_PREFIXES: tuple[str, ...] = (
    "text/",
    "image/",
    "application/pdf",
    "application/json",
    "application/msword",
    "application/vnd.ms-",
    "application/vnd.openxmlformats-officedocument.",
    "application/rtf",
    "application/x-tar",
    "application/zip",
    "application/gzip",
    "application/x-gzip",
    "application/octet-stream",  # last-resort; UI can flag unknowns
)

_ROLE_STRENGTH = {"owner": 4, "admin": 3, "editor": 2, "viewer": 1, "member": 0}


# ---------------------------------------------------------------------------
# Result payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentView:
    document: Document
    uploader_display_name: str
    source_kind: str  # "local" for uploads-with-no-source or when we resolve it
    source_name: str


@dataclass(frozen=True)
class DocumentPage:
    """One page of ``list_documents_in_space`` results.

    ``total`` is the pre-pagination row count *after* filters are
    applied — the UI uses it to render "X of Y" and to size its
    pagination control.
    """

    items: list[DocumentView]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class DocumentStats:
    """Aggregate roll-up for a single space's document library.

    Only carries what the Knowledge grid + Space detail page actually
    render. Everything is derived from ``documents`` — no external
    joins needed.
    """

    total: int
    total_size_bytes: int
    last_uploaded_at: Optional[str]  # ISO-8601 or None
    by_source: dict[str, int]  # source_kind → count
    by_mime_group: dict[str, int]  # normalised mime bucket → count
    by_indexing_state: dict[str, int]  # not_indexed / indexing / indexed / failed


# Whitelisted ``order_by`` values. The API surface is deliberately small so
# arbitrary column injection can't happen through the query-string.
_ORDER_BY_COLUMNS = {
    "created_at": Document.created_at,
    "name": Document.name,
    "size_bytes": Document.size_bytes,
    "updated_at": Document.updated_at,
}


def _mime_bucket(mime: str) -> str:
    """Group a raw mime type into a UI-friendly bucket.

    Keeps the ``by_mime_group`` dict small even in libraries with a lot
    of variety. The buckets match the file-type icons the frontend
    already renders.
    """

    m = (mime or "").lower()
    if m.startswith("image/"):
        return "image"
    if m == "application/pdf":
        return "pdf"
    if "word" in m or "opendocument.text" in m:
        return "word"
    if "sheet" in m or "excel" in m or "opendocument.spreadsheet" in m:
        return "spreadsheet"
    if "presentation" in m or "powerpoint" in m or "opendocument.presentation" in m:
        return "presentation"
    if m.startswith("text/") or m == "application/json":
        return "text"
    return "other"


# The file-type groups a library can be filtered by. Same names as the
# ``by_mime_group`` keys in the stats, so the UI uses one vocabulary.
FILE_TYPES = ("image", "pdf", "word", "spreadsheet", "presentation", "text", "other")


def _bucket_conditions(column):
    """SQL twin of ``_mime_bucket``: one condition per bucket, in the same
    first-match-wins order."""

    m = func.lower(func.coalesce(column, ""))
    return [
        ("image", m.like("image/%")),
        ("pdf", m == "application/pdf"),
        ("word", or_(m.like("%word%"), m.like("%opendocument.text%"))),
        (
            "spreadsheet",
            or_(
                m.like("%sheet%"),
                m.like("%excel%"),
                m.like("%opendocument.spreadsheet%"),
            ),
        ),
        (
            "presentation",
            or_(
                m.like("%presentation%"),
                m.like("%powerpoint%"),
                m.like("%opendocument.presentation%"),
            ),
        ),
        ("text", or_(m.like("text/%"), m == "application/json")),
    ]


def file_type_clause(column, file_type: str):
    """Condition selecting the documents whose mime type falls in ``file_type``.

    Raises ``DocumentValidationError`` for an unknown group so a typo cannot
    silently show everything (or nothing).
    """

    if file_type not in FILE_TYPES:
        raise DocumentValidationError(f"unknown file type: {file_type!r}")
    earlier = []
    for name, cond in _bucket_conditions(column):
        if name == file_type:
            return and_(cond, *[not_(c) for c in earlier])
        earlier.append(cond)
    # "other": matches none of the named groups.
    return and_(*[not_(c) for c in earlier])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_filename(name: str) -> str:
    """Strip path separators and control chars. Enforce max length."""

    trimmed = name.strip()
    if not trimmed:
        raise DocumentValidationError("filename is required")
    # Reject bare path traversals and NULs. Everything else — including
    # unicode — is fine as a display name.
    cleaned = re.sub(r"[\x00-\x1f/\\]", "_", trimmed)
    if len(cleaned) > MAX_FILENAME_LENGTH:
        raise DocumentValidationError(
            f"filename exceeds {MAX_FILENAME_LENGTH} chars"
        )
    return cleaned


def _validate_mime(mime: str) -> str:
    normalised = mime.strip().lower() or "application/octet-stream"
    for allowed in _ALLOWED_MIME_PREFIXES:
        if allowed.endswith("/") and normalised.startswith(allowed):
            return normalised
        if normalised == allowed:
            return normalised
        # Prefixes that stop at a "-" (application/vnd.ms-) or a "." (the
        # Office Open XML family) match any type that starts with them. The "."
        # case was missing, which made every .docx/.xlsx/.pptx upload fail.
        if allowed.endswith(("-", ".")) and normalised.startswith(allowed):
            return normalised
    raise UnsupportedMimeError(f"unsupported mime type: {mime!r}")


async def _caller_space_role(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
) -> Optional[str]:
    """Return the highest role code the caller has on the space, or None."""

    codes = await resolve_effective_role_codes(
        session,
        user_id=user_id,
        organization_id=organization_id,
        resource_type="space",
        resource_id=space_id,
    )
    # Space owner check: if the space is a personal space owned directly
    # by this user, they always have owner semantics even without an
    # explicit binding.
    space = (
        await session.execute(
            select(Space).where(
                Space.id == space_id,
                Space.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if space is None:
        return None
    if space.owner_type == "user" and space.owner_id == user_id:
        codes = codes | {"owner"}
    if not codes:
        return None
    role = max(codes, key=lambda c: _ROLE_STRENGTH.get(c, -1))
    if (
        role in ("owner", "admin")
        and space.preset != "personal"
        and await access_guard.user_is_restricted_owner(
            session, organization_id=organization_id, user_id=user_id
        )
    ):
        # The organisation owner of a team or enterprise organisation is not a space
        # administrator, however the role reached them (a group, a unit, an old grant).
        return "editor" if _ROLE_STRENGTH.get(role, -1) >= _ROLE_STRENGTH["editor"] else role
    return role


def _at_least(role: Optional[str], required: str) -> bool:
    return (
        role is not None
        and _ROLE_STRENGTH.get(role, -1) >= _ROLE_STRENGTH[required]
    )


async def _read_gate(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
) -> tuple[bool, doc_acl.DocAccess]:
    """What this person may read in one space: ``(whole_space, document_rules)``.

    ``whole_space`` is true when their role lets them read the space; then only the
    documents hidden from them are subtracted. Otherwise they may read only the
    documents shared with them on their own, and when there are none the space
    does not exist for them (404-ish, like any space they cannot see).
    """

    role = await _caller_space_role(
        session, user_id=user_id, organization_id=organization_id, space_id=space_id
    )
    access = await doc_acl.load_doc_access(
        session, user_id=user_id, organization_id=organization_id
    )
    whole = _at_least(role, "viewer")
    if not whole and space_id not in access.granted.values():
        # 404-ish: hide existence rather than leak.
        raise NotFoundError(f"space not accessible: {space_id}")
    return whole, access


def _require_visible(
    doc: Document, *, whole_space: bool, access: doc_acl.DocAccess
) -> None:
    """Raise ``NotFoundError`` unless this document may be read under ``_read_gate``."""

    if doc.id in access.denied or not (whole_space or doc.id in access.granted):
        raise NotFoundError(f"document not found: {doc.id}")
async def caller_has_space_role(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    required: str,
) -> bool:
    """True when the caller's effective role on the space is at least ``required``.

    Same rule the upload path uses, exposed so other services (for example
    the Feishu folder connection) can ask "may this person add documents here?"
    without reaching into private helpers. A space that does not exist in the
    organisation counts as no access.
    """

    role = await _caller_space_role(
        session,
        user_id=user_id,
        organization_id=organization_id,
        space_id=space_id,
    )
    return _at_least(role, required)


async def _to_view(
    session: AsyncSession, doc: Document
) -> DocumentView:
    # Uploader display name (best-effort).
    uploader_name = ""
    if doc.uploaded_by_user_id is not None:
        rows = await session.execute(
            select(User.display_name).where(User.id == doc.uploaded_by_user_id)
        )
        row = rows.scalar_one_or_none()
        if row is not None:
            uploader_name = row

    # Source info; if the FK was set-null (source deleted), we fall back
    # to ``local`` so the UI can still render a sensible badge.
    source_kind = "local"
    source_name = document_sources.DEFAULT_LOCAL_SOURCE_NAME
    if doc.source_id is not None:
        src_view = None
        try:
            src_view = await document_sources.get_source(
                session,
                source_id=doc.source_id,
                organization_id=doc.organization_id,
            )
        except NotFoundError:
            pass
        if src_view is not None:
            source_kind = src_view.source.kind
            source_name = src_view.source.name

    return DocumentView(
        document=doc,
        uploader_display_name=uploader_name,
        source_kind=source_kind,
        source_name=source_name,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_documents_in_space(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    caller_user_id: uuid.UUID,
    include_archived: bool = False,
    q: Optional[str] = None,
    source_kind: Optional[str] = None,
    file_type: Optional[str] = None,
    order_by: str = "created_at",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> DocumentPage:
    """Paginated + filterable listing for the space knowledge browser.

    ``q`` matches ``name`` case-insensitively (``ILIKE %q%``). ``source_kind``
    filters against the resolved connector kind — we join ``document_sources``
    only when this filter is set to keep the common "list everything" path
    on a single-table scan.

    ``file_type`` is one of ``FILE_TYPES`` (image, pdf, word, spreadsheet,
    presentation, text, other); an unknown value raises
    ``DocumentValidationError``.

    ``order_by`` is validated against a whitelist; anything else falls back
    to ``created_at``.
    """

    whole_space, access = await _read_gate(
        session,
        user_id=caller_user_id,
        organization_id=organization_id,
        space_id=space_id,
    )

    # Clamp inputs so a bad caller can't ask for a 100k-row page.
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    order = "desc" if str(order).lower() == "desc" else "asc"

    base = select(Document).where(
        Document.organization_id == organization_id,
        Document.space_id == space_id,
        doc_acl.visibility_clause(access, whole_space=whole_space),
    )
    if not include_archived:
        base = base.where(Document.status != "archived")
    if q:
        pattern = f"%{q.strip()}%"
        base = base.where(Document.name.ilike(pattern))
    if file_type:
        base = base.where(file_type_clause(Document.mime_type, file_type))
    if source_kind:
        # ``source_kind`` is not on the ``documents`` row directly — it lives
        # on ``document_sources``. Local uploads may also have ``source_id``
        # NULL (older data), in which case we treat them as ``local``.
        from quberi_api.domain.documents.models import DocumentSource

        if source_kind == "local":
            base = base.outerjoin(
                DocumentSource, DocumentSource.id == Document.source_id
            ).where(
                (Document.source_id.is_(None))
                | (DocumentSource.kind == "local")
            )
        else:
            base = base.join(
                DocumentSource, DocumentSource.id == Document.source_id
            ).where(DocumentSource.kind == source_kind)

    # Total *after* filters, before pagination.
    total_stmt = select(func.count()).select_from(base.subquery())
    total = int((await session.execute(total_stmt)).scalar_one() or 0)

    order_col = _ORDER_BY_COLUMNS.get(order_by, Document.created_at)
    ordering = order_col.desc() if order == "desc" else order_col.asc()

    page_stmt = base.order_by(ordering).limit(limit).offset(offset)
    rows = (await session.execute(page_stmt)).scalars().all()
    items = [await _to_view(session, r) for r in rows]
    return DocumentPage(items=items, total=total, limit=limit, offset=offset)


async def stats_for_space(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    caller_user_id: uuid.UUID,
) -> DocumentStats:
    """Aggregate roll-up that the Knowledge grid card can render without
    pulling the full document list. Excludes archived rows.
    """

    whole_space, access = await _read_gate(
        session,
        user_id=caller_user_id,
        organization_id=organization_id,
        space_id=space_id,
    )

    from quberi_api.domain.documents.models import DocumentSource

    active = (
        select(Document)
        .where(
            Document.organization_id == organization_id,
            Document.space_id == space_id,
            Document.status != "archived",
            doc_acl.visibility_clause(access, whole_space=whole_space),
        )
        .subquery()
    )

    # Overall counts.
    top = await session.execute(
        select(
            func.count(active.c.id),
            func.coalesce(func.sum(active.c.size_bytes), 0),
            func.max(active.c.created_at),
        )
    )
    total, size_sum, last_at = top.one()

    # by_source — a two-way case: resolved from ``document_sources.kind`` if
    # available, else falling back to ``local`` for orphaned rows.
    by_source_stmt = (
        select(
            case(
                (DocumentSource.kind.is_(None), "local"),
                else_=DocumentSource.kind,
            ).label("kind"),
            func.count(active.c.id),
        )
        .select_from(
            active.outerjoin(
                DocumentSource, DocumentSource.id == active.c.source_id
            )
        )
        .group_by("kind")
    )
    by_source = {
        kind: int(count)
        for kind, count in (await session.execute(by_source_stmt)).all()
    }

    # by_indexing_state — grouped straight off the base table.
    by_state_stmt = select(
        active.c.indexing_state, func.count(active.c.id)
    ).group_by(active.c.indexing_state)
    by_indexing_state = {
        state: int(count)
        for state, count in (await session.execute(by_state_stmt)).all()
    }

    # by_mime bucket — pulled row-wise then bucketed in Python; keeps
    # the SQL portable (no CASE-per-mime chain).
    mime_rows = await session.execute(select(active.c.mime_type))
    by_mime_group: dict[str, int] = {}
    for (mime,) in mime_rows.all():
        bucket = _mime_bucket(mime or "")
        by_mime_group[bucket] = by_mime_group.get(bucket, 0) + 1

    return DocumentStats(
        total=int(total or 0),
        total_size_bytes=int(size_sum or 0),
        last_uploaded_at=last_at.isoformat() if last_at is not None else None,
        by_source=by_source,
        by_mime_group=by_mime_group,
        by_indexing_state=by_indexing_state,
    )


async def get_document(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    caller_user_id: uuid.UUID,
) -> DocumentView:
    whole_space, access = await _read_gate(
        session,
        user_id=caller_user_id,
        organization_id=organization_id,
        space_id=space_id,
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
    _require_visible(doc, whole_space=whole_space, access=access)
    return await _to_view(session, doc)


# ---------------------------------------------------------------------------
# Upload (local)
# ---------------------------------------------------------------------------


async def upload_local(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    uploader_user_id: uuid.UUID,
    filename: str,
    file_obj: BinaryIO,
    content_type: str,
    attributes: Optional[dict] = None,
) -> DocumentView:
    """Accept a multipart upload, store to MinIO, insert a Document row.

    ``attributes`` is stored on the row as given (the guest sandbox uses it to
    tag an upload with its owner and expiry).
    """

    role = await _caller_space_role(
        session,
        user_id=uploader_user_id,
        organization_id=organization_id,
        space_id=space_id,
    )
    if not _at_least(role, "editor"):
        raise DocumentPermissionError(
            "editor role or higher is required to upload documents to this space"
        )

    filename = _sanitize_filename(filename)
    mime = _validate_mime(content_type)

    # Ensure the local source row exists (idempotent).
    local_source = await document_sources.ensure_local_source(
        session, organization_id=organization_id
    )
    # ``ensure_local_source`` runs its own flush; commit so its
    # transaction resolves before we begin the upload transaction.
    await session.commit()

    doc_id = uuid.uuid4()
    storage_key = local_connector.storage_key_for(
        organization_id=str(organization_id),
        space_id=str(space_id),
        document_id=str(doc_id),
        filename=filename,
    )

    stored = await local_connector.store(
        storage_key=storage_key,
        source_stream=file_obj,
        content_type=mime,
        max_bytes=MAX_UPLOAD_BYTES,
    )

    async with session.begin():
        doc = Document(
            id=doc_id,
            organization_id=organization_id,
            space_id=space_id,
            source_id=local_source.id,
            uploaded_by_user_id=uploader_user_id,
            name=filename,
            mime_type=mime,
            size_bytes=stored.size_bytes,
            storage_key=stored.storage_key,
            checksum_sha256=stored.checksum_sha256,
            external_ref={},
            sync_state="local_only",
            status="uploaded",
            attributes=dict(attributes or {}),
        )
        session.add(doc)
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=uploader_user_id,
                action="document.uploaded",
                resource_type="document",
                resource_id=doc_id,
                space_id=space_id,
                payload={
                    "name": filename,
                    "mime": mime,
                    "size_bytes": stored.size_bytes,
                    "checksum_sha256": stored.checksum_sha256,
                },
            )
        )
        await session.flush()
        await session.refresh(
            doc, attribute_names=["created_at", "updated_at", "indexing_state"]
        )

    # Kick off the RAG indexing pipeline asynchronously. A failed enqueue
    # only logs — the upload itself has already committed, and the user
    # can retry indexing from the UI. The DB default keeps the row at
    # ``indexing_state='not_indexed'`` until the worker flips it.
    await enqueue_index_document(doc.id)

    return await _to_view(session, doc)


# ---------------------------------------------------------------------------
# Save a text note (e.g. a snippet captured from an external agent chat)
# ---------------------------------------------------------------------------

# Keep note titles short enough to live inside the ``.md`` filename without
# tripping the filename-length guard (``MAX_FILENAME_LENGTH``) once the
# ``.md`` suffix is appended.
MAX_NOTE_TITLE_LENGTH = 180
MAX_NOTE_TEXT_BYTES = 1 * 1024 * 1024  # 1 MiB of UTF-8 text


def _compose_note_markdown(
    *,
    title: str,
    text: str,
    source_label: Optional[str],
    source_url: Optional[str],
    saved_at: datetime,
) -> str:
    """Render a note as a self-describing markdown document.

    The front matter is plain markdown (not YAML) so the existing text
    extractor indexes it verbatim — the source label and timestamp become
    searchable context, not hidden metadata.
    """

    lines = [f"# {title}", ""]
    provenance: list[str] = []
    if source_label:
        provenance.append(f"来源 / Source: {source_label}")
    if source_url:
        provenance.append(f"链接 / Link: {source_url}")
    provenance.append(f"保存时间 / Saved: {saved_at.isoformat()}")
    lines.extend(provenance)
    lines.extend(["", "---", "", text.strip(), ""])
    return "\n".join(lines)


async def save_note(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    author_user_id: uuid.UUID,
    title: str,
    text: str,
    source_label: Optional[str] = None,
    source_url: Optional[str] = None,
) -> DocumentView:
    """Store a plain-text note into a space as a markdown document.

    This is the write side of the knowledge base used by external agents
    (e.g. the ``dsh-quberi`` DeepSeek Harness plugin's ``quberi_save``
    tool): a user captures a conclusion from a chat and it lands in their
    knowledge base, searchable alongside uploaded files.

    It is a thin wrapper over :func:`upload_local` — the note text is
    composed into a markdown blob and pushed through the exact same path
    (MinIO store, Document row, editor+ permission check, async indexing),
    so a note is indistinguishable from an uploaded ``.md`` file
    downstream. Nothing new is invented in the storage/index/permission
    layers.
    """

    clean_title = (title or "").strip()
    if not clean_title:
        raise DocumentValidationError("note title must not be empty")
    if len(clean_title) > MAX_NOTE_TITLE_LENGTH:
        clean_title = clean_title[:MAX_NOTE_TITLE_LENGTH].rstrip()

    if not (text or "").strip():
        raise DocumentValidationError("note text must not be empty")

    markdown = _compose_note_markdown(
        title=clean_title,
        text=text,
        source_label=source_label,
        source_url=source_url,
        saved_at=datetime.now(timezone.utc),
    )
    blob = markdown.encode("utf-8")
    if len(blob) > MAX_NOTE_TEXT_BYTES:
        raise DocumentValidationError(
            f"note is too large ({len(blob)} bytes); "
            f"limit is {MAX_NOTE_TEXT_BYTES} bytes"
        )

    attributes = {
        "origin": "note",
        "source_label": source_label or "",
        "source_url": source_url or "",
    }

    return await upload_local(
        session,
        organization_id=organization_id,
        space_id=space_id,
        uploader_user_id=author_user_id,
        filename=f"{clean_title}.md",
        file_obj=io.BytesIO(blob),
        content_type="text/markdown",
        attributes=attributes,
    )


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def delete_document(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    caller_user_id: uuid.UUID,
) -> None:
    role = await _caller_space_role(
        session,
        user_id=caller_user_id,
        organization_id=organization_id,
        space_id=space_id,
    )
    if role is None:
        raise NotFoundError(f"space not accessible: {space_id}")

    # We authorise on either "admin+" OR "you own it" — the latter lets
    # editors clean up their own uploads without needing admin.
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
    # A document hidden from this person cannot be acted on either.
    denied = (
        await doc_acl.load_doc_access(
            session, user_id=caller_user_id, organization_id=organization_id
        )
    ).denied
    if doc.id in denied:
        raise NotFoundError(f"document not found: {document_id}")

    is_own = doc.uploaded_by_user_id == caller_user_id
    if not (is_own or _at_least(role, "admin")):
        raise DocumentPermissionError(
            "you can only delete documents you uploaded, unless you're a space admin"
        )

    storage_key = doc.storage_key
    doc_id_for_cascade = doc.id
    doc_org_id = doc.organization_id
    # The authz check above already ran ``session.execute`` which
    # auto-begins a transaction; commit it so we can open a fresh one
    # below without a "transaction is already begun" error.
    await session.commit()
    async with session.begin():
        await session.delete(doc)
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=caller_user_id,
                action="document.deleted",
                resource_type="document",
                resource_id=doc.id,
                space_id=space_id,
                payload={"name": doc.name, "storage_key": storage_key},
            )
        )

    # Best-effort blob delete after the DB commits — if the blob delete
    # fails we've still consistently removed the metadata and can rely
    # on a future garbage collector to reap orphaned objects.
    if storage_key:
        try:
            await local_connector.delete(storage_key)
        except Exception:  # pragma: no cover — logged upstream
            pass

    # Cascade to the vector store. We resolve the org's *current*
    # embedding model to know which Qdrant collection to prune; if that
    # resolution fails (no binding, worker not warm) we swallow — an
    # orphaned point in Qdrant is a soft leak the RAG ops page (H-3+)
    # can garbage-collect later.
    try:
        from quberi_api.infrastructure.qdrant import create_qdrant_client
        from quberi_api.core.config import get_settings
        from quberi_api.services.ai import resolve_embedding_provider
        from quberi_api.services.rag import remove_document_from_index

        resolved = await resolve_embedding_provider(
            session, organization_id=doc_org_id
        )
        qdrant = create_qdrant_client(get_settings())
        try:
            await remove_document_from_index(
                qdrant=qdrant,
                document_id=doc_id_for_cascade,
                model_id=resolved.model.model_id,
                vector_dim=resolved.model.vector_dim or 1024,
            )
        finally:
            await qdrant.close()
    except Exception:  # pragma: no cover — logged, DB truth already correct
        pass

    # A picture keeps its vector in a collection of its own. Removed on its own
    # path so a failure above (no text-embedding binding, say) cannot skip it.
    # If this too fails, a search still drops the hit: it checks every result
    # against the database, and this document's row is gone.
    try:
        from quberi_api.core.config import get_settings
        from quberi_api.infrastructure.qdrant import create_qdrant_client
        from quberi_api.services.rag.indexer import remove_image_from_index

        qdrant = create_qdrant_client(get_settings())
        try:
            await remove_image_from_index(qdrant=qdrant, document_id=doc_id_for_cascade)
        finally:
            await qdrant.close()
    except Exception:  # pragma: no cover — logged, DB truth already correct
        pass


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


async def get_document_text(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    caller_user_id: uuid.UUID,
) -> str:
    """Re-extract and return the document's full plain text.

    We never persist the extracted text (only Qdrant chunk previews +
    the original blob are kept), so a citation that wants to render the
    exact highlighted span in its source document re-runs the same
    deterministic extractor the indexer used. ``char_start``/``char_end``
    recorded on the citation at index time are offsets into *this exact*
    extraction, so re-extracting is safe as long as the blob hasn't
    changed since indexing (same ``storage_key`` / checksum).
    """

    whole_space, access = await _read_gate(
        session,
        user_id=caller_user_id,
        organization_id=organization_id,
        space_id=space_id,
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
    _require_visible(doc, whole_space=whole_space, access=access)
    if doc.storage_key is None:
        raise NotFoundError(
            "document has no local blob (reference-only source; not yet supported)"
        )

    from quberi_api.services.rag.extract import extract_text

    chunks: list[bytes] = []
    async for piece in local_connector.open_stream(doc.storage_key):
        chunks.append(piece)
    blob = b"".join(chunks)
    extracted = extract_text(blob=blob, filename=doc.name, mime_type=doc.mime_type)
    return extracted.text


async def open_download_stream(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    caller_user_id: uuid.UUID,
) -> tuple[Document, AsyncIterator[bytes]]:
    """Return the document row + a byte-stream ready for ``StreamingResponse``.

    The API layer sets ``Content-Type`` and
    ``Content-Disposition: attachment; filename=...`` from the returned
    document metadata.
    """

    whole_space, access = await _read_gate(
        session,
        user_id=caller_user_id,
        organization_id=organization_id,
        space_id=space_id,
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
    _require_visible(doc, whole_space=whole_space, access=access)
    if doc.storage_key is None:
        # ``reference_only`` sources would need an external connector to
        # stream — not supported in Slice E.
        raise NotFoundError(
            "document has no local blob (reference-only source; not yet supported)"
        )

    stream = local_connector.open_stream(doc.storage_key)
    return doc, stream
