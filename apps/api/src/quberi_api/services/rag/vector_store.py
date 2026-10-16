"""Qdrant façade — pins one collection per embedding model.

Collections
-----------
A Qdrant collection has a fixed ``vector_dim``. We can't share one
across embedding models with different dimensionalities, and switching
a running model would require re-indexing anyway. So:

    quberi_chunks__{sanitised_model_id}     e.g. quberi_chunks__bge_m3

Each point payload carries the tenant + resource ids so retrieval
can filter server-side::

    {
      "organization_id": "…",
      "space_id":        "…",
      "document_id":     "…",
      "chunk_index":     3,
      "text":            "…first 400 chars for preview…",
      "checksum":        "…doc sha256…",
    }

Retrieval always filters by ``space_id`` — the RAG scope for a
conversation is a *set* of accessible space ids — and the API-side
authorisation layer *also* re-checks the caller's live role on each
retrieved space (defence in depth against stale bindings).

The Qdrant client we get from ``infrastructure.qdrant`` is process-wide
so instances of :class:`VectorStore` are cheap.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels

_log = logging.getLogger(__name__)

_COLLECTION_PREFIX = "quberi_chunks__"


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkPoint:
    """One (chunk × vector × payload) row destined for Qdrant."""

    chunk_index: int
    text: str
    vector: list[float]
    # Character offsets into the document's extracted plain text (see
    # ``rag.chunk.Chunk``). Persisted so a citation can later be mapped
    # back onto a highlighted span in the re-extracted text without
    # redoing the chunking math.
    char_start: int
    char_end: int


@dataclass(frozen=True)
class RetrievedChunk:
    """A hit from :meth:`VectorStore.search`."""

    document_id: uuid.UUID
    space_id: uuid.UUID
    # Original upload name, preserved in Qdrant payload so the LLM can
    # distinguish a parent notice from an attachment whose own body title
    # differs (e.g. "通知（附件2）" vs. "建设运营工作方案").
    document_name: str
    chunk_index: int
    text: str
    score: float
    # Character offsets into the document's extracted plain text — lets
    # the UI map a citation onto a highlighted span in the source doc.
    char_start: int
    char_end: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def collection_name_for_model(model_id: str, prefix: str = _COLLECTION_PREFIX) -> str:
    """Compute a stable Qdrant collection name for an embedding model.

    ``bge-m3`` → ``quberi_chunks__bge_m3``; ``text-embedding-3-small`` →
    ``quberi_chunks__text_embedding_3_small``. The mapping must be
    reversible-by-eyeball so ops can find the right collection during
    debugging. Image vectors use their own prefix (see
    :mod:`quberi_api.services.rag.image_store`).
    """

    sanitised = re.sub(r"[^a-z0-9]+", "_", model_id.lower()).strip("_")
    return f"{prefix}{sanitised}"


def _point_id_for(document_id: uuid.UUID, chunk_index: int) -> str:
    """Deterministic id — re-indexing a doc overwrites its old points.

    Qdrant point ids must be either an int or a UUID; we derive a UUID5
    from the (doc_id, chunk_index) pair so the same chunk always lands
    on the same id across re-indexing.
    """

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{document_id}:{chunk_index}"))


# ---------------------------------------------------------------------------
# Façade
# ---------------------------------------------------------------------------


class VectorStore:
    """Thin wrapper around ``AsyncQdrantClient``.

    Callers typically get an instance via
    ``VectorStore(client, model_id, vector_dim)`` and reuse it for the
    lifetime of one indexing job or one search request. Instantiation
    is free; the underlying HTTP client is shared.
    """

    # Subclasses that keep a different kind of vector (pictures) override this
    # so their collection never mixes with the text one.
    collection_prefix = _COLLECTION_PREFIX

    def __init__(
        self,
        client: AsyncQdrantClient,
        *,
        model_id: str,
        vector_dim: int,
    ) -> None:
        self._client = client
        self._model_id = model_id
        self._collection = collection_name_for_model(model_id, self.collection_prefix)
        self._vector_dim = vector_dim

    @property
    def collection(self) -> str:
        return self._collection

    async def ensure_collection(self) -> None:
        """Create the collection on first use — idempotent, race-safe.

        Qdrant's ``create_collection`` raises if the collection exists;
        checking ``collection_exists`` first is not atomic across
        replicas but on our single-instance dev topology it's fine, and
        the ``recreate_collection=False`` semantics match what we want
        (never destroy live data).
        """

        exists = await self._client.collection_exists(self._collection)
        if exists:
            return
        _log.info(
            "creating Qdrant collection %s (dim=%d, model=%s)",
            self._collection, self._vector_dim, self._model_id,
        )
        await self._client.create_collection(
            collection_name=self._collection,
            vectors_config=qmodels.VectorParams(
                size=self._vector_dim,
                distance=qmodels.Distance.COSINE,
            ),
        )
        # Payload indexes — enable server-side filtering without scanning
        # every point. Skip organization_id for now (we filter at the
        # binding layer above); space_id is the hot path.
        await self._client.create_payload_index(
            collection_name=self._collection,
            field_name="space_id",
            field_schema=qmodels.PayloadSchemaType.KEYWORD,
        )
        await self._client.create_payload_index(
            collection_name=self._collection,
            field_name="document_id",
            field_schema=qmodels.PayloadSchemaType.KEYWORD,
        )
        await self._client.create_payload_index(
            collection_name=self._collection,
            field_name="organization_id",
            field_schema=qmodels.PayloadSchemaType.KEYWORD,
        )

    async def upsert_document(
        self,
        *,
        organization_id: uuid.UUID,
        space_id: uuid.UUID,
        document_id: uuid.UUID,
        document_name: str,
        document_checksum: Optional[str],
        chunks: Sequence[ChunkPoint],
    ) -> None:
        """Replace all points for a document.

        We delete-then-upsert so a re-index that produces *fewer* chunks
        doesn't leave orphaned stale points behind.
        """

        if not chunks:
            await self.delete_document(document_id=document_id)
            return

        await self.ensure_collection()
        await self.delete_document(document_id=document_id)

        points = [
            qmodels.PointStruct(
                id=_point_id_for(document_id, c.chunk_index),
                vector=c.vector,
                payload={
                    "organization_id": str(organization_id),
                    "space_id": str(space_id),
                    "document_id": str(document_id),
                    "document_name": document_name,
                    "document_checksum": document_checksum or "",
                    "chunk_index": c.chunk_index,
                    "text": c.text,
                    "char_start": c.char_start,
                    "char_end": c.char_end,
                },
            )
            for c in chunks
        ]
        await self._client.upsert(
            collection_name=self._collection, points=points, wait=True
        )

    async def delete_document(self, *, document_id: uuid.UUID) -> None:
        """Remove every point for the document (no-op if collection missing)."""

        if not await self._client.collection_exists(self._collection):
            return
        await self._client.delete(
            collection_name=self._collection,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="document_id",
                            match=qmodels.MatchValue(value=str(document_id)),
                        )
                    ]
                )
            ),
            wait=True,
        )

    async def count_document(self, *, document_id: uuid.UUID) -> int:
        """How many points the store still holds for a document (0 if no collection).

        Used to *verify* a delete: ``delete_document`` removes them, but the
        caller that must be certain (the guest sandbox cleanup) checks.
        """

        if not await self._client.collection_exists(self._collection):
            return 0
        result = await self._client.count(
            collection_name=self._collection,
            count_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="document_id",
                        match=qmodels.MatchValue(value=str(document_id)),
                    )
                ]
            ),
            exact=True,
        )
        return int(result.count)

    async def search(
        self,
        *,
        query_vector: list[float],
        space_ids: Iterable[uuid.UUID],
        organization_id: uuid.UUID,
        limit: int = 12,
        document_ids: Iterable[uuid.UUID] | None = None,
    ) -> list[RetrievedChunk]:
        """Top-K semantic search restricted to a set of accessible spaces.

        ``document_ids``, when given, further restricts hits to those documents
        (filtered inside the vector store, so other documents in the same space
        cannot crowd the result set). An empty collection means "none", not
        "no filter".

        Empty ``space_ids`` short-circuits to an empty list — the
        caller shouldn't get points from spaces they can't see even if
        their query embeds identically to something in another space.
        """

        space_id_list = [str(s) for s in space_ids]
        if not space_id_list:
            return []
        doc_id_list = None if document_ids is None else [str(d) for d in document_ids]
        if doc_id_list is not None and not doc_id_list:
            return []
        if not await self._client.collection_exists(self._collection):
            return []
        must: list[Any] = [
            qmodels.FieldCondition(
                key="organization_id",
                match=qmodels.MatchValue(value=str(organization_id)),
            ),
            qmodels.FieldCondition(
                key="space_id", match=qmodels.MatchAny(any=space_id_list)
            ),
        ]
        if doc_id_list is not None:
            must.append(
                qmodels.FieldCondition(
                    key="document_id", match=qmodels.MatchAny(any=doc_id_list)
                )
            )
        response = await self._client.query_points(
            collection_name=self._collection,
            query=query_vector,
            query_filter=qmodels.Filter(must=must),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        # ``QueryResponse.points`` is an empty list on a valid zero-hit
        # search. Do NOT use `or response` here: an empty list is falsy,
        # and falling back to the Pydantic response object makes iteration
        # yield field tuples (`('points', [])`), causing the tuple.payload
        # crash seen in the stream error path.
        hits = getattr(response, "points", None)
        if hits is None:
            hits = response
        results: list[RetrievedChunk] = []
        for pt in hits:
            payload = pt.payload or {}
            try:
                results.append(
                    RetrievedChunk(
                        document_id=uuid.UUID(payload["document_id"]),
                        space_id=uuid.UUID(payload["space_id"]),
                        document_name=str(payload.get("document_name", "")),
                        chunk_index=int(payload.get("chunk_index", 0)),
                        text=str(payload.get("text", "")),
                        score=float(pt.score or 0.0),
                        char_start=int(payload.get("char_start", 0)),
                        char_end=int(payload.get("char_end", 0)),
                    )
                )
            except (KeyError, ValueError):  # pragma: no cover — defensive
                _log.warning("skipping malformed Qdrant payload: %s", payload)
        return results
