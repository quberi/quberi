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

# (client, collection) pairs this process has already made sure have the ``codes`` index.
# The client is part of the key so a collection dropped and made again on another client
# (a test, an operator rebuilding) is checked again rather than assumed.
_CODES_INDEXED: set[tuple[int, str]] = set()


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
    # Where the text came from: "text" is the document's own text; "image_ocr" is text
    # read out of a picture. A point without the field (everything indexed before it
    # existed) is "text".
    source_kind: str = "text"
    # Exact keys of the part numbers and dimensions in the text (see ``codes.py``), so a
    # query that contains one can be looked up exactly instead of by meaning. Empty for
    # ordinary text.
    codes: tuple[str, ...] = ()
    # Where in the source document the text sits (see ``chunk.Chunk``): first and last
    # page and block. ``None`` when the extractor did not record positions.
    page_start: int | None = None
    page_end: int | None = None
    unit_start: int | None = None
    unit_end: int | None = None
    # "text_block" or "ocr_block" (any part recognised from a picture); "" if unknown.
    unit_label: str = ""


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
    source_kind: str = "text"
    page_start: int | None = None
    page_end: int | None = None
    unit_start: int | None = None
    unit_end: int | None = None
    unit_label: str = ""
    # Which version of the document the text was read from (1 for everything indexed
    # before versions existed), and the newest version the document has now. The second
    # is not stored in the index; retrieval fills it in from the database so an answer can
    # say that a passage comes from a version that has since been replaced.
    version_number: int = 1
    latest_version: int | None = None


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


def _position_payload(c: ChunkPoint) -> dict[str, Any]:
    """The position fields that are known, left out when they are not."""

    fields = {
        "page_start": c.page_start,
        "page_end": c.page_end,
        "unit_start": c.unit_start,
        "unit_end": c.unit_end,
    }
    known = {k: v for k, v in fields.items() if v is not None}
    if c.unit_label:
        known["unit_label"] = c.unit_label
    return known


def _optional_int(value: Any) -> int | None:
    """A payload number, or ``None`` when the field is absent or not a whole number."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _positions_from(payload: dict[str, Any]) -> dict[str, Any]:
    """Where a hit sits and which version of the document it was read from."""

    version = _optional_int(payload.get("version_number"))
    return {
        "version_number": version if version and version > 0 else 1,
        "page_start": _optional_int(payload.get("page_start")),
        "page_end": _optional_int(payload.get("page_end")),
        "unit_start": _optional_int(payload.get("unit_start")),
        "unit_end": _optional_int(payload.get("unit_end")),
        "unit_label": str(payload.get("unit_label") or ""),
    }


def _point_id_for(document_id: uuid.UUID, chunk_index: int, version_number: int = 1) -> str:
    """Deterministic id — re-indexing a doc overwrites its old points.

    Qdrant point ids must be either an int or a UUID; we derive a UUID5
    from the (doc_id, chunk_index) pair so the same chunk always lands
    on the same id across re-indexing. A later version of a document gets ids of its
    own, so indexing it never overwrites the points of an earlier one. Version 1 keeps
    the id it always had, which is also the id of every point indexed before versions.
    """

    if version_number <= 1:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{document_id}:{chunk_index}"))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{document_id}:v{version_number}:{chunk_index}"))


def _document_filter(
    document_id: uuid.UUID, version_number: int | None, source_kind: str | None = None
) -> qmodels.Filter:
    """Points of one document: all of them, or those read from one version.

    ``source_kind`` narrows to the points of one kind ("image_ocr", "image_caption"): the
    text read out of a picture and the description of it are filed separately, so redoing
    one leaves the other alone.

    A point without a ``version_number`` was indexed before versions existed and is
    version 1, so version 1 is "no number, or a number below 2" and any later version is
    an exact match.
    """

    must: list[Any] = [
        qmodels.FieldCondition(key="document_id", match=qmodels.MatchValue(value=str(document_id)))
    ]
    must_not: list[Any] = []
    if source_kind is not None:
        must.append(
            qmodels.FieldCondition(key="source_kind", match=qmodels.MatchValue(value=source_kind))
        )
    if version_number is not None:
        if version_number <= 1:
            must_not.append(
                qmodels.FieldCondition(key="version_number", range=qmodels.Range(gte=2))
            )
        else:
            must.append(
                qmodels.FieldCondition(
                    key="version_number", match=qmodels.MatchValue(value=version_number)
                )
            )
    return qmodels.Filter(must=must, must_not=must_not or None)


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
            await self._ensure_codes_index()
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
        await self._client.create_payload_index(
            collection_name=self._collection,
            field_name="codes",
            field_schema=qmodels.PayloadSchemaType.KEYWORD,
        )

    async def _ensure_codes_index(self) -> None:
        """Make sure an older collection also has the ``codes`` index.

        Collections made before exact code matching existed were created without it.
        Creating an index that is already there is a no-op on the server, and the process
        remembers which collections it has checked so this costs one call per collection,
        not one per document.
        """

        key = (id(self._client), self._collection)
        if key in _CODES_INDEXED:
            return
        await self._client.create_payload_index(
            collection_name=self._collection,
            field_name="codes",
            field_schema=qmodels.PayloadSchemaType.KEYWORD,
        )
        _CODES_INDEXED.add(key)

    async def upsert_document(
        self,
        *,
        organization_id: uuid.UUID,
        space_id: uuid.UUID,
        document_id: uuid.UUID,
        document_name: str,
        document_checksum: Optional[str],
        chunks: Sequence[ChunkPoint],
        version_number: int = 1,
        keep_other_versions: bool = True,
        replace_source_kind: str | None = None,
    ) -> None:
        """Replace the points of one version of a document.

        We delete-then-upsert so a re-index that produces *fewer* chunks
        doesn't leave orphaned stale points behind. Only the points of
        ``version_number`` are replaced: an earlier version of the document stays
        searchable, labelled with its version. ``keep_other_versions=False`` replaces
        everything the document has in the index (a picture, which is searched by what
        it shows now). ``replace_source_kind`` replaces only the points of that kind, so
        the text read out of a picture and the description of it do not erase each other.
        """

        scope = None if not keep_other_versions else version_number
        if not chunks:
            await self.delete_document(
                document_id=document_id, version_number=scope, source_kind=replace_source_kind
            )
            return

        await self.ensure_collection()
        await self.delete_document(
            document_id=document_id, version_number=scope, source_kind=replace_source_kind
        )

        points = [
            qmodels.PointStruct(
                id=_point_id_for(document_id, c.chunk_index, version_number),
                vector=c.vector,
                payload={
                    "organization_id": str(organization_id),
                    "space_id": str(space_id),
                    "document_id": str(document_id),
                    "document_name": document_name,
                    "document_checksum": document_checksum or "",
                    "version_number": max(1, version_number),
                    "chunk_index": c.chunk_index,
                    "text": c.text,
                    "char_start": c.char_start,
                    "char_end": c.char_end,
                    "source_kind": c.source_kind,
                    **({"codes": list(c.codes)} if c.codes else {}),
                    **_position_payload(c),
                },
            )
            for c in chunks
        ]
        await self._client.upsert(
            collection_name=self._collection, points=points, wait=True
        )

    async def delete_document(
        self,
        *,
        document_id: uuid.UUID,
        version_number: int | None = None,
        source_kind: str | None = None,
    ) -> None:
        """Remove the points of a document (no-op if collection missing).

        Every version's points by default; with ``version_number``, only that version's;
        with ``source_kind``, only the points of that kind.
        """

        if not await self._client.collection_exists(self._collection):
            return
        await self._client.delete(
            collection_name=self._collection,
            points_selector=qmodels.FilterSelector(
                filter=_document_filter(document_id, version_number, source_kind)
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

    @staticmethod
    def _access_filter(
        *,
        space_ids: Iterable[uuid.UUID],
        organization_id: uuid.UUID,
        document_ids: Iterable[uuid.UUID] | None = None,
        granted_document_ids: Iterable[uuid.UUID] | None = None,
        denied_document_ids: Iterable[uuid.UUID] | None = None,
    ) -> qmodels.Filter | None:
        """The conditions that decide which points a person may be shown.

        One definition, used by every kind of lookup (by meaning, by exact code), so two
        ways of searching can never disagree about who may see what. ``None`` means the
        scope is empty and nothing may be returned.
        """

        space_id_list = [str(s) for s in space_ids]
        granted_list = [str(d) for d in (granted_document_ids or [])]
        if not space_id_list and not granted_list:
            return None
        doc_id_list = None if document_ids is None else [str(d) for d in document_ids]
        if doc_id_list is not None and not doc_id_list:
            return None
        reachable: list[Any] = []
        if space_id_list:
            reachable.append(
                qmodels.FieldCondition(
                    key="space_id", match=qmodels.MatchAny(any=space_id_list)
                )
            )
        if granted_list:
            reachable.append(
                qmodels.FieldCondition(
                    key="document_id", match=qmodels.MatchAny(any=granted_list)
                )
            )
        must: list[Any] = [
            qmodels.FieldCondition(
                key="organization_id",
                match=qmodels.MatchValue(value=str(organization_id)),
            ),
            # In a readable space, OR a document shared on its own.
            qmodels.Filter(should=reachable),
        ]
        must_not: list[Any] = []
        denied_list = [str(d) for d in (denied_document_ids or [])]
        if denied_list:
            must_not.append(
                qmodels.FieldCondition(
                    key="document_id", match=qmodels.MatchAny(any=denied_list)
                )
            )
        if doc_id_list is not None:
            must.append(
                qmodels.FieldCondition(
                    key="document_id", match=qmodels.MatchAny(any=doc_id_list)
                )
            )
        return qmodels.Filter(must=must, must_not=must_not or None)

    async def search_by_codes(
        self,
        *,
        codes: Sequence[str],
        space_ids: Iterable[uuid.UUID],
        organization_id: uuid.UUID,
        limit: int = 12,
        document_ids: Iterable[uuid.UUID] | None = None,
        granted_document_ids: Iterable[uuid.UUID] | None = None,
        denied_document_ids: Iterable[uuid.UUID] | None = None,
    ) -> list[RetrievedChunk]:
        """Points whose text contains one of these part numbers or dimensions, exactly.

        Same access conditions as :meth:`search` (it builds them with the same function),
        with "similar to this vector" replaced by "has this code". The score is the number
        of the asked-for codes the point carries, so a point that has two of the three
        codes outranks one that has one. ``codes`` are keys from :mod:`codes`.
        """

        wanted = [c for c in dict.fromkeys(codes) if c]
        if not wanted:
            return []
        built = self._access_filter(
            space_ids=space_ids,
            organization_id=organization_id,
            document_ids=document_ids,
            granted_document_ids=granted_document_ids,
            denied_document_ids=denied_document_ids,
        )
        if built is None or not await self._client.collection_exists(self._collection):
            return []
        built.must.append(
            qmodels.FieldCondition(key="codes", match=qmodels.MatchAny(any=wanted))
        )
        # Fetch generously: the ranking by how many codes matched happens here.
        points, _ = await self._client.scroll(
            collection_name=self._collection,
            scroll_filter=built,
            limit=max(limit * 4, 20),
            with_payload=True,
            with_vectors=False,
        )
        results: list[tuple[int, RetrievedChunk]] = []
        for pt in points:
            payload = pt.payload or {}
            try:
                matched = len(set(payload.get("codes") or []) & set(wanted))
                results.append(
                    (
                        matched,
                        RetrievedChunk(
                            document_id=uuid.UUID(payload["document_id"]),
                            space_id=uuid.UUID(payload["space_id"]),
                            document_name=str(payload.get("document_name", "")),
                            chunk_index=int(payload.get("chunk_index", 0)),
                            text=str(payload.get("text", "")),
                            score=float(matched),
                            char_start=int(payload.get("char_start", 0)),
                            char_end=int(payload.get("char_end", 0)),
                            source_kind=str(payload.get("source_kind", "text")),
                            **_positions_from(payload),
                        ),
                    )
                )
            except (KeyError, ValueError):  # pragma: no cover — defensive
                _log.warning("skipping malformed Qdrant payload: %s", payload)
        results.sort(key=lambda pair: (-pair[0], str(pair[1].document_id), pair[1].chunk_index))
        return [chunk for _, chunk in results[:limit]]

    async def search(
        self,
        *,
        query_vector: list[float],
        space_ids: Iterable[uuid.UUID],
        organization_id: uuid.UUID,
        limit: int = 12,
        document_ids: Iterable[uuid.UUID] | None = None,
        granted_document_ids: Iterable[uuid.UUID] | None = None,
        denied_document_ids: Iterable[uuid.UUID] | None = None,
    ) -> list[RetrievedChunk]:
        """Top-K semantic search restricted to a set of accessible spaces.

        Document-level rules are applied inside the index, not afterwards:
        ``granted_document_ids`` are searched *in addition to* ``space_ids`` (documents
        shared with the person from spaces they cannot read as a whole), and
        ``denied_document_ids`` are always excluded, even inside a readable space.

        ``document_ids``, when given, further restricts hits to those documents
        (filtered inside the vector store, so other documents in the same space
        cannot crowd the result set). An empty collection means "none", not
        "no filter".

        Empty ``space_ids`` short-circuits to an empty list — the
        caller shouldn't get points from spaces they can't see even if
        their query embeds identically to something in another space.
        """

        built = self._access_filter(
            space_ids=space_ids,
            organization_id=organization_id,
            document_ids=document_ids,
            granted_document_ids=granted_document_ids,
            denied_document_ids=denied_document_ids,
        )
        if built is None:
            return []
        if not await self._client.collection_exists(self._collection):
            return []
        response = await self._client.query_points(
            collection_name=self._collection,
            query=query_vector,
            query_filter=built,
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
                        source_kind=str(payload.get("source_kind", "text")),
                        **_positions_from(payload),
                    )
                )
            except (KeyError, ValueError):  # pragma: no cover — defensive
                _log.warning("skipping malformed Qdrant payload: %s", payload)
        return results
