"""End-to-end indexing pipeline for one document.

Flow
----
1. Load the ``Document`` row and check preconditions (status='uploaded',
   has a ``storage_key``).
2. Mark it ``indexing_state='indexing'`` and commit — the UI can now
   show "processing" and any concurrent second job for the same doc
   will observe the state and bail.
3. Download the blob from MinIO.
4. Extract text via :mod:`extract`. (A picture skips steps 4-8: it is
   turned into one vector by the image model and stored in the picture
   collection instead; see :func:`_index_image`.)
5. Chunk via :mod:`chunk`.
6. Resolve the org's embedding provider + model.
7. Embed all chunks in a single batch (or as many batches as the
   provider needs).
8. Upsert into Qdrant, keyed by (document_id, chunk_index).
9. Mark the row ``indexed`` with ``chunk_count`` + ``last_indexed_at``.

Errors at any step flip the row to ``indexing_state='failed'`` with the
error stashed in ``indexing_error``. The worker isn't expected to retry
automatically — a human clicks "reindex" from the UI, or the RAG ops
page (H-3+) can bulk-retry all failed rows.
"""

from __future__ import annotations

import asyncio
import io
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from qdrant_client import AsyncQdrantClient

from quberi_api.core.config import get_settings
from quberi_api.domain.documents.models import Document
from quberi_api.services.ai import resolve_embedding_provider
from quberi_api.services.ai.local_image_embedding import (
    IMAGE_MODEL_DIMS,
    ImageDecodeError,
    get_image_embedder,
    image_vector_dim,
)
from quberi_api.services.documents import versions as versions_service
from quberi_api.services.documents.connectors import local as local_connector
from quberi_api.services.rag.chunk import Source, chunk_text, chunk_units
from quberi_api.services.rag.extract import ExtractError, TextUnit, extract_text
from quberi_api.services.rag.image_store import ImageVectorStore
from quberi_api.services.rag.vector_store import ChunkPoint, VectorStore

_log = logging.getLogger(__name__)


# Embed in batches to avoid huge single requests. Provider-specific
# tuning can be layered on later.
EMBED_BATCH_SIZE = 32

# ``documents.attributes[IMAGE_INDEX_MODEL_KEY]`` names the picture model whose vector
# a picture currently has, so a change of model can find what to re-index.
IMAGE_INDEX_MODEL_KEY = "image_index_model"
LEGACY_IMAGE_MODEL = "bge-vl-base"


# ---------------------------------------------------------------------------
# Result payload
# ---------------------------------------------------------------------------


class ReviewPendingError(RuntimeError):
    """The document has not been approved, so nothing of it may be indexed."""


@dataclass(frozen=True)
class IndexingResult:
    document_id: uuid.UUID
    chunk_count: int
    collection: str
    model_id: str


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def index_document(
    ctx: dict[str, Any] | None,
    document_id: str,
) -> dict:
    """arq job entry.

    The ``ctx`` dict is populated by the worker's ``on_startup`` hook
    with the shared session factory + qdrant client. The signature is
    positional-first so arq's enqueue-with-args calling convention (which
    passes only the job args) works cleanly.
    """

    if ctx is None:
        raise RuntimeError("index_document must be called with an arq ctx")
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    qdrant: AsyncQdrantClient = ctx["qdrant"]

    doc_uuid = uuid.UUID(document_id)
    async with session_factory() as session:
        try:
            result = await _run_pipeline(session, qdrant=qdrant, document_id=doc_uuid)
        except ReviewPendingError:
            # Waiting for (or refused) approval: approving queues a fresh job.
            return {"document_id": document_id, "skipped": "not_approved"}
    return {
        "document_id": str(result.document_id),
        "chunk_count": result.chunk_count,
        "collection": result.collection,
        "model_id": result.model_id,
    }


async def reindex_document(
    session: AsyncSession,
    *,
    qdrant: AsyncQdrantClient,
    document_id: uuid.UUID,
) -> IndexingResult:
    """Synchronous re-index — used by the REST reindex endpoint.

    Same pipeline as :func:`index_document` but reuses the caller's
    session so the response can wait for completion. For large docs
    this can take a while; the reindex endpoint is deliberately
    admin-only to discourage casual triggering.
    """

    return await _run_pipeline(session, qdrant=qdrant, document_id=document_id)


async def remove_document_from_index(
    *,
    qdrant: AsyncQdrantClient,
    document_id: uuid.UUID,
    model_id: str,
    vector_dim: int,
) -> None:
    """Best-effort cleanup on delete. Missing collection → no-op."""

    store = VectorStore(qdrant, model_id=model_id, vector_dim=vector_dim)
    try:
        await store.delete_document(document_id=document_id)
    except Exception:  # pragma: no cover — logged, doc row already gone
        _log.exception("qdrant delete_document failed for %s", document_id)


async def remove_image_from_index(
    *,
    qdrant: AsyncQdrantClient,
    document_id: uuid.UUID,
    model_id: str | None = None,
) -> None:
    """Best-effort removal of a picture's vector. Missing collection → no-op.

    Without ``model_id`` the vector is removed under **every** picture model this
    build knows: a picture indexed before a model change keeps its old vector until
    it is re-indexed, and deleting the file must not leave that behind.
    """

    models = [model_id] if model_id else list(IMAGE_MODEL_DIMS)
    for model in models:
        store = ImageVectorStore(qdrant, model_id=model, vector_dim=image_vector_dim(model))
        try:
            await store.delete_document(document_id=document_id)
        except Exception:  # pragma: no cover — logged, doc row already gone
            _log.exception("qdrant image delete_document failed for %s (%s)", document_id, model)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


async def _run_pipeline(
    session: AsyncSession,
    *,
    qdrant: AsyncQdrantClient,
    document_id: uuid.UUID,
) -> IndexingResult:
    doc = (
        await session.execute(
            select(Document).where(Document.id == document_id)
        )
    ).scalar_one_or_none()
    if doc is None:
        raise RuntimeError(f"document {document_id} not found")
    if doc.status == "archived":
        raise RuntimeError(f"document {document_id} is archived; refusing to index")
    if doc.review_state != "approved":
        raise ReviewPendingError(f"document {document_id} is not approved; not indexing")
    # ``reference_only`` docs carry no storage_key — they'll come with
    # pre-fetched text via :func:`index_document_from_text` instead.
    if not doc.storage_key:
        raise RuntimeError(
            f"document {document_id} has no storage_key; use "
            "index_document_from_text() for reference-only sources"
        )

    # The version being indexed is fixed now: an upload of a newer version while this job
    # runs must not have its points filed under the older number (or the reverse).
    version_number = await versions_service.current_version_number(session, doc)

    # -- 1. Fetch blob → 2. Extract → 3. Chunk (only for mirrored docs) --
    doc.indexing_state = "indexing"
    doc.indexing_error = None
    await session.commit()
    _log.info("indexing %s (%s)", doc.id, doc.name)

    try:
        blob = await _download_blob(doc.storage_key)
        if is_picture(doc.mime_type):
            return await _index_image(session, qdrant=qdrant, doc=doc, blob=blob)
        try:
            extracted = extract_text(
                blob=blob, filename=doc.name, mime_type=doc.mime_type
            )
        except ExtractError as exc:
            await _mark_failed(session, doc, f"extract: {exc}")
            raise

        units = extracted.units
        text = extracted.text
        if any(u.needs_ocr for u in units):
            units, text = await _read_scanned_pages(
                session, doc=doc, blob=blob, extracted=extracted
            )

        return await _finalise_index_from_text(
            session, qdrant=qdrant, doc=doc, text=text, units=units,
            version_number=version_number,
        )
    except Exception as exc:  # noqa: BLE001
        if doc.indexing_state != "failed":
            await _mark_failed(session, doc, f"{type(exc).__name__}: {exc}")
        raise


def is_picture(mime_type: str | None) -> bool:
    """Whether a document is filed as a picture (and so indexed by what it shows)."""

    return (mime_type or "").lower().startswith("image/")


async def _index_image(
    session: AsyncSession,
    *,
    qdrant: AsyncQdrantClient,
    doc: Document,
    blob: bytes,
) -> IndexingResult:
    """Turn one picture into one vector and file it in the picture collection.

    The picture is decoded in memory only; nothing is written anywhere but the
    vector (with the ids that keep it inside its organisation and space).
    """

    settings = get_settings()
    if not settings.image_search_enabled:
        await _mark_failed(session, doc, "image: picture search is turned off on this server")
        raise RuntimeError("image search is disabled")
    model_id = settings.default_image_embedding_model
    try:
        vectors = await get_image_embedder(model_id).embed_images([blob])
    except ImageDecodeError as exc:
        await _mark_failed(session, doc, f"image: {exc}")
        raise
    store = ImageVectorStore(qdrant, model_id=model_id, vector_dim=image_vector_dim(model_id))
    await store.upsert_image(
        organization_id=doc.organization_id,
        space_id=doc.space_id,
        document_id=doc.id,
        document_name=doc.name,
        mime_type=doc.mime_type,
        document_checksum=doc.checksum_sha256,
        vector=vectors[0],
    )
    # Which model made this vector. A picture indexed before models were recorded was
    # made by the first one (bge-vl-base).
    previous_model = (doc.attributes or {}).get(IMAGE_INDEX_MODEL_KEY, LEGACY_IMAGE_MODEL)
    doc.attributes = {**(doc.attributes or {}), IMAGE_INDEX_MODEL_KEY: model_id}
    doc.indexing_state = "indexed"
    doc.chunk_count = 1
    doc.last_indexed_at = datetime.now(timezone.utc)
    doc.indexing_error = None
    await session.commit()
    if previous_model != model_id and previous_model in IMAGE_MODEL_DIMS:
        # Moved to a new model: drop the vector the old one left behind.
        await remove_image_from_index(qdrant=qdrant, document_id=doc.id, model_id=previous_model)
    _log.info("indexed picture %s in %s (model=%s)", doc.id, store.collection, model_id)
    await _queue_picture_text(doc)
    await _queue_picture_caption(session, doc)
    return IndexingResult(
        document_id=doc.id, chunk_count=1, collection=store.collection, model_id=model_id
    )


OCR_ATTRIBUTE_KEY = "pdf_ocr"


async def _read_scanned_pages(
    session: AsyncSession, *, doc: Document, blob: bytes, extracted: Any
) -> tuple[Sequence[TextUnit], str]:
    """Read the PDF pages that have no text layer, and return all units and their text.

    Three outcomes, each deliberate:

    * the pages are read: their text joins the rest as ``ocr_block`` units;
    * OCR cannot run and **nothing** in the file has text: the document fails with a plain
      reason (the same outcome a scan always had), never an empty "indexed" document;
    * OCR cannot run but **some** pages have text: those are indexed, and the number of
      pages not read is kept on the document so it can be shown.

    Nothing here lets a failure to read a page fail a document that has other pages.
    """

    from quberi_api.services.ai.ocr import OcrError, get_ocr_provider
    from quberi_api.services.rag.image_text import save_attribute
    from quberi_api.services.rag.pdf_ocr import read_scanned_pages

    units = list(extracted.units)
    flagged = sum(1 for u in units if u.needs_ocr)
    has_text = any(u.text.strip() for u in units)
    try:
        provider = get_ocr_provider()
    except OcrError as exc:
        return await _scan_not_read(
            session, doc=doc, units=units, flagged=flagged, has_text=has_text, reason=str(exc),
        )

    filled, outcome = await read_scanned_pages(blob, units, provider)
    still_unread = sum(1 for u in filled if u.needs_ocr)
    if outcome.read == 0 and not has_text:
        return await _scan_not_read(
            session, doc=doc, units=units, flagged=flagged, has_text=False,
            reason="; ".join(outcome.errors) or "no text was found on any page",
        )
    await save_attribute(
        session, doc.id, OCR_ATTRIBUTE_KEY,
        {
            "pages_read": outcome.read,
            "pages_unread": still_unread,
            "pages_failed": outcome.failed,
            "pages_over_limit": outcome.skipped,
            "out_of_time": outcome.out_of_time,
            "errors": list(outcome.errors),
        },
    )
    return filled, "\n\n".join(u.text for u in filled if u.text.strip())


async def _scan_not_read(
    session: AsyncSession,
    *,
    doc: Document,
    units: list[TextUnit],
    flagged: int,
    has_text: bool,
    reason: str,
) -> tuple[Sequence[TextUnit], str]:
    if not has_text:
        message = f"extract: the PDF has no text layer and could not be read ({reason})"
        await _mark_failed(session, doc, message)
        raise ExtractError(message)
    from quberi_api.services.rag.image_text import save_attribute

    await save_attribute(
        session, doc.id, OCR_ATTRIBUTE_KEY,
        {"pages_read": 0, "pages_unread": flagged, "pages_failed": 0,
         "pages_over_limit": 0, "errors": [reason]},
    )
    return units, "\n\n".join(u.text for u in units if u.text.strip())


async def _queue_picture_text(doc: Document) -> None:
    """Ask for the text inside a picture to be read and filed, after the picture itself
    is already searchable. Never raises: the picture's own index is not undone by this."""

    if not get_settings().image_ocr_enabled:
        return
    try:
        from quberi_api.workers.enqueue import enqueue_image_text

        await enqueue_image_text(doc.id, doc.checksum_sha256)
    except Exception:  # noqa: BLE001
        _log.exception("could not queue the text of picture %s", doc.id)


async def _queue_picture_caption(session: AsyncSession, doc: Document) -> None:
    """Ask for a picture to be described by the organisation's vision model, if it has
    turned that on. Nothing is queued otherwise, so nothing can be sent. Never raises."""

    try:
        from quberi_api.services.rag.image_caption import descriptions_enabled

        if not await descriptions_enabled(session, doc.organization_id):
            return
        from quberi_api.workers.enqueue import enqueue_image_caption

        await enqueue_image_caption(doc.id, doc.checksum_sha256)
    except Exception:  # noqa: BLE001
        _log.exception("could not queue the description of picture %s", doc.id)


async def index_document_from_text(
    session: AsyncSession,
    *,
    qdrant: AsyncQdrantClient,
    document_id: uuid.UUID,
    text: str,
) -> IndexingResult:
    """Index a document whose text is already in memory.

    Used by connectors that don't cache the binary in MinIO
    (``caching_policy='reference_only'``) — the Feishu docs sync path
    pulls raw text via the Feishu API and hands it here directly.
    """

    doc = (
        await session.execute(
            select(Document).where(Document.id == document_id)
        )
    ).scalar_one_or_none()
    if doc is None:
        raise RuntimeError(f"document {document_id} not found")
    if doc.status == "archived":
        raise RuntimeError(f"document {document_id} is archived; refusing to index")
    if doc.review_state != "approved":
        raise ReviewPendingError(f"document {document_id} is not approved; not indexing")
    doc.indexing_state = "indexing"
    doc.indexing_error = None
    await session.commit()
    _log.info("indexing %s (%s) from in-memory text", doc.id, doc.name)

    try:
        return await _finalise_index_from_text(
            session, qdrant=qdrant, doc=doc, text=text
        )
    except Exception as exc:  # noqa: BLE001
        if doc.indexing_state != "failed":
            await _mark_failed(session, doc, f"{type(exc).__name__}: {exc}")
        raise


async def _finalise_index_from_text(
    session: AsyncSession,
    *,
    qdrant: AsyncQdrantClient,
    doc: Document,
    text: str,
    units: Sequence[TextUnit] = (),
    version_number: int = 1,
) -> IndexingResult:
    """Steps 4-8 of the pipeline: chunk → embed → upsert → mark ready.

    Both the mirrored path (blob → extract → this) and the
    reference-only path (fetch text via connector → this) converge
    here. Keeps the "embed + Qdrant + DB state" logic in one place.
    """

    # -- 4. Chunk ------------------------------------------------------
    # Text that knows where it came from keeps that through chunking; everything else
    # (other formats, text fetched from a connector) is chunked exactly as it always was.
    positioned = [
        (u.text, Source(u.page, u.unit_index, u.unit_label, u.unit_end, u.section))
        for u in units
        if u.text.strip()
    ]
    chunks = chunk_units(positioned) if positioned else chunk_text(text or "")
    if not chunks:
        await _mark_failed(session, doc, "chunking produced zero windows")
        raise RuntimeError("chunker returned no chunks")

    # -- 5. Resolve embedding provider (per-org) -----------------------
    resolved = await resolve_embedding_provider(
        session, organization_id=doc.organization_id
    )
    embedder = resolved.provider
    model_id = resolved.model.model_id
    vector_dim = resolved.model.vector_dim or embedder.vector_dim

    # -- 6. Embed in batches -------------------------------------------
    texts = [c.text for c in chunks]
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[start : start + EMBED_BATCH_SIZE]
        vecs = await embedder.embed(batch, model=model_id)
        vectors.extend(vecs)

    # -- 7. Upsert to Qdrant -------------------------------------------
    store = VectorStore(qdrant, model_id=model_id, vector_dim=vector_dim)
    await store.upsert_document(
        organization_id=doc.organization_id,
        space_id=doc.space_id,
        document_id=doc.id,
        document_name=doc.name,
        document_checksum=doc.checksum_sha256,
        version_number=version_number,
        chunks=[
            ChunkPoint(
                chunk_index=c.index,
                text=c.text,
                vector=v,
                char_start=c.char_start,
                char_end=c.char_end,
                page_start=c.page_start,
                page_end=c.page_end,
                unit_start=c.unit_start,
                unit_end=c.unit_end,
                unit_label=c.unit_label,
                section=c.section,
            )
            for c, v in zip(chunks, vectors)
        ],
    )

    # -- 8. Mark indexed -----------------------------------------------
    # If a newer version was uploaded while this one was being read, this job has filed
    # an older version (still correct, still searchable as that version) but the document
    # now shows another file: leave its state to the job that is indexing that one.
    await session.refresh(doc, attribute_names=["current_version_id"])
    if await versions_service.current_version_number(session, doc) != version_number:
        _log.info("indexed %s version %d, which is no longer current", doc.id, version_number)
        return IndexingResult(
            document_id=doc.id,
            chunk_count=len(chunks),
            collection=store.collection,
            model_id=model_id,
        )
    doc.indexing_state = "indexed"
    doc.chunk_count = len(chunks)
    doc.last_indexed_at = datetime.now(timezone.utc)
    doc.indexing_error = None
    await session.commit()
    _log.info(
        "indexed %s: %d chunks in %s (model=%s)",
        doc.id, len(chunks), store.collection, model_id,
    )
    return IndexingResult(
        document_id=doc.id,
        chunk_count=len(chunks),
        collection=store.collection,
        model_id=model_id,
    )


async def _download_blob(storage_key: str) -> bytes:
    """Buffer a whole MinIO object into memory.

    OK for the current 25 MB cap; a giant-file path (H-3) will need to
    stream through the extractor instead.
    """

    buf = io.BytesIO()
    async for chunk in local_connector.open_stream(storage_key):
        buf.write(chunk)
    return buf.getvalue()


async def _mark_failed(session: AsyncSession, doc: Document, message: str) -> None:
    doc.indexing_state = "failed"
    doc.indexing_error = message[:1024]
    doc.chunk_count = 0
    doc.last_indexed_at = None
    await session.commit()
