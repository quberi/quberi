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
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from qdrant_client import AsyncQdrantClient

from quberi_api.core.config import get_settings
from quberi_api.domain.documents.models import Document
from quberi_api.services.ai import resolve_embedding_provider
from quberi_api.services.ai.local_image_embedding import (
    BGE_VL_DIM,
    ImageDecodeError,
    get_image_embedder,
)
from quberi_api.services.documents.connectors import local as local_connector
from quberi_api.services.rag.chunk import chunk_text
from quberi_api.services.rag.extract import ExtractError, extract_text
from quberi_api.services.rag.image_store import ImageVectorStore
from quberi_api.services.rag.vector_store import ChunkPoint, VectorStore

_log = logging.getLogger(__name__)


# Embed in batches to avoid huge single requests. Provider-specific
# tuning can be layered on later.
EMBED_BATCH_SIZE = 32


# ---------------------------------------------------------------------------
# Result payload
# ---------------------------------------------------------------------------


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
        result = await _run_pipeline(session, qdrant=qdrant, document_id=doc_uuid)
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
    """Best-effort removal of a picture's vector. Missing collection → no-op."""

    model = model_id or get_settings().default_image_embedding_model
    store = ImageVectorStore(qdrant, model_id=model, vector_dim=BGE_VL_DIM)
    try:
        await store.delete_document(document_id=document_id)
    except Exception:  # pragma: no cover — logged, doc row already gone
        _log.exception("qdrant image delete_document failed for %s", document_id)


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

    # ``reference_only`` docs carry no storage_key — they'll come with
    # pre-fetched text via :func:`index_document_from_text` instead.
    if not doc.storage_key:
        raise RuntimeError(
            f"document {document_id} has no storage_key; use "
            "index_document_from_text() for reference-only sources"
        )

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

        return await _finalise_index_from_text(
            session, qdrant=qdrant, doc=doc, text=extracted.text
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
    store = ImageVectorStore(qdrant, model_id=model_id, vector_dim=BGE_VL_DIM)
    await store.upsert_image(
        organization_id=doc.organization_id,
        space_id=doc.space_id,
        document_id=doc.id,
        document_name=doc.name,
        mime_type=doc.mime_type,
        document_checksum=doc.checksum_sha256,
        vector=vectors[0],
    )
    doc.indexing_state = "indexed"
    doc.chunk_count = 1
    doc.last_indexed_at = datetime.now(timezone.utc)
    doc.indexing_error = None
    await session.commit()
    _log.info("indexed picture %s in %s (model=%s)", doc.id, store.collection, model_id)
    return IndexingResult(
        document_id=doc.id, chunk_count=1, collection=store.collection, model_id=model_id
    )


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
) -> IndexingResult:
    """Steps 4-8 of the pipeline: chunk → embed → upsert → mark ready.

    Both the mirrored path (blob → extract → this) and the
    reference-only path (fetch text via connector → this) converge
    here. Keeps the "embed + Qdrant + DB state" logic in one place.
    """

    # -- 4. Chunk ------------------------------------------------------
    chunks = chunk_text(text or "")
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
        chunks=[
            ChunkPoint(
                chunk_index=c.index,
                text=c.text,
                vector=v,
                char_start=c.char_start,
                char_end=c.char_end,
            )
            for c, v in zip(chunks, vectors)
        ],
    )

    # -- 8. Mark indexed -----------------------------------------------
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
