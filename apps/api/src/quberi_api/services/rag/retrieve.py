"""Retrieval — query-time RAG for chat.

Two responsibilities, kept separate from the generation layer:

1. **Compute the caller's scoped space set** — the intersection of:
   * spaces the caller has *live* viewer+ access to (recomputed every
     query, never cached — this is Quberi's headline promise)
   * the conversation's scope declaration (auto = all accessible;
     custom = anchor space + explicit ``ConversationKnowledgeSource``s)
2. **Search the vector store** for that space set and return top-K
   ``RetrievedChunk``s, ready to be numbered as citations.

We deliberately do the space-set computation *and* the Qdrant filter
*and* a post-retrieval per-chunk membership check. Belt + braces:
Qdrant's filter is fast, but a bug or a stale index could leak; the
Python re-check is authoritative.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Sequence

from qdrant_client import AsyncQdrantClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.conversations.models import (
    Conversation,
    ConversationKnowledgeSource,
)
from quberi_api.domain.ai.models import AiModelSelection
from quberi_api.services.ai import resolve_embedding_provider
from quberi_api.services.ai.base import EmbeddingProvider
from quberi_api.services.ai.registry import MODEL_REGISTRY
from quberi_api.services.authz import list_accessible_space_ids
from quberi_api.services.rag.vector_store import RetrievedChunk, VectorStore

_log = logging.getLogger(__name__)


DEFAULT_TOP_K = 8


@dataclass(frozen=True)
class ScopedRetrieval:
    """The full retrieval result for one query.

    ``space_ids`` is the effective scope that produced ``chunks`` — a
    query that finds nothing (empty ``chunks``) is still informative,
    because the caller may want to render "you can see N spaces but none
    of them had matches".
    """

    chunks: list[RetrievedChunk]
    space_ids: list[uuid.UUID]
    scope_mode: str
    model_id: str
    collection: str


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------


async def effective_scope_for_conversation(
    session: AsyncSession,
    *,
    conversation: Conversation,
    caller_user_id: uuid.UUID,
) -> tuple[list[uuid.UUID], str]:
    """The spaces a conversation may search right now, and its scope mode.

    Always starts from the caller's live permissions. A ``custom`` conversation
    then keeps only the spaces the person picked that they can *still* read (an
    intersection); ``auto`` is everything they can read. Shared by text and
    picture retrieval so both look in exactly the same places.
    """

    org_id = conversation.organization_id

    # 1. Live accessible spaces for the caller.
    live_accessible = await list_accessible_space_ids(
        session, user_id=caller_user_id, organization_id=org_id
    )

    # 2. Apply the conversation's scope declaration.
    scope_mode = conversation.scope_mode or "auto"
    if scope_mode == "custom":
        # Custom scope searches EXACTLY the spaces the user selected via the
        # composer "#" menu (stored as ConversationKnowledgeSource rows).
        # The conversation's anchor/home space is NOT special here — it is
        # searched only if the user kept it selected. An empty selection
        # means "search nothing" (answer from the model alone).
        declared: set[uuid.UUID] = set()
        rows = (
            await session.execute(
                select(ConversationKnowledgeSource.space_id).where(
                    ConversationKnowledgeSource.conversation_id == conversation.id
                )
            )
        ).all()
        for (sid,) in rows:
            declared.add(sid)
        # Intersect with live access — a source the caller lost access
        # to is silently excluded from retrieval (still shown in the
        # management surface via list_scope_sources).
        effective: list[uuid.UUID] = sorted(
            (s for s in declared if s in live_accessible), key=str
        )
    else:  # auto
        effective = sorted(live_accessible, key=str)
    return effective, scope_mode


async def retrieve_for_conversation(
    session: AsyncSession,
    *,
    qdrant: AsyncQdrantClient,
    conversation: Conversation,
    caller_user_id: uuid.UUID,
    query: str,
    top_k: int = DEFAULT_TOP_K,
) -> ScopedRetrieval:
    """End-to-end retrieval for a specific conversation + question.

    Never raises for "empty scope" or "no chunks" — those are legitimate
    empty results the caller should convey to the LLM as "I don't have
    supporting documents". Only misconfiguration (no embedding provider
    for the org, Qdrant down) raises.
    """

    org_id = conversation.organization_id
    effective, scope_mode = await effective_scope_for_conversation(
        session, conversation=conversation, caller_user_id=caller_user_id
    )

    chunks, model_id, collection = await _search_scoped_spaces(
        session,
        qdrant=qdrant,
        organization_id=org_id,
        space_ids=effective,
        query=query,
        top_k=top_k,
    )
    return ScopedRetrieval(
        chunks=chunks,
        space_ids=effective,
        scope_mode=scope_mode,
        model_id=model_id,
        collection=collection,
    )


async def retrieve_for_user(
    session: AsyncSession,
    *,
    qdrant: AsyncQdrantClient,
    organization_id: uuid.UUID,
    caller_user_id: uuid.UUID,
    query: str,
    top_k: int = DEFAULT_TOP_K,
) -> ScopedRetrieval:
    """Conversation-free retrieval over *all* spaces the caller can see.

    This is the entry point the agent's ``search_knowledge`` tool uses:
    there's no ``Conversation`` to scope against, so the effective set is
    simply the caller's live-accessible spaces (the "auto" scope). Same
    live-permission recompute + defensive re-check as the conversation
    path — the agent can never retrieve from a space its caller can't see.
    """

    live_accessible = await list_accessible_space_ids(
        session, user_id=caller_user_id, organization_id=organization_id
    )
    effective = sorted(live_accessible, key=str)
    chunks, model_id, collection = await _search_scoped_spaces(
        session,
        qdrant=qdrant,
        organization_id=organization_id,
        space_ids=effective,
        query=query,
        top_k=top_k,
    )
    return ScopedRetrieval(
        chunks=chunks,
        space_ids=effective,
        scope_mode="auto",
        model_id=model_id,
        collection=collection,
    )


async def retrieve_with_scope(
    session: AsyncSession,
    *,
    qdrant: AsyncQdrantClient,
    organization_id: uuid.UUID,
    caller_user_id: uuid.UUID,
    query: str,
    scope_mode: str,
    scope_space_ids: Sequence[uuid.UUID],
    top_k: int = DEFAULT_TOP_K,
    document_ids: Sequence[uuid.UUID] | None = None,
) -> ScopedRetrieval:
    """Agent-aware retrieval honouring a per-agent knowledge scope.

    ``document_ids`` additionally narrows the search to specific documents
    (used by the guest assistant to search only a visitor's own uploads).

    ``scope_mode``:

    * ``user`` — every space the caller can access (permission-faithful,
      identical to :func:`retrieve_for_user`).
    * ``intersect`` — ``scope_space_ids`` ∩ the caller's accessible spaces.
      Still permission-faithful: a configured space the caller can't see is
      dropped.
    * ``fixed`` — exactly ``scope_space_ids``, **independent of the caller's
      own access**. This deliberately lets the agent read spaces the caller
      could not read by hand, so it must only be exposed to a restricted
      audience (enforced at the visibility layer, not here).
    """

    org_id = organization_id
    configured = [s for s in (scope_space_ids or [])]

    if scope_mode == "fixed":
        effective = sorted(set(configured), key=str)
    else:
        accessible = await list_accessible_space_ids(
            session, user_id=caller_user_id, organization_id=org_id
        )
        if scope_mode == "intersect":
            allow = set(configured)
            effective = sorted((s for s in accessible if s in allow), key=str)
        else:  # "user" (and any unknown value → safe default)
            effective = sorted(accessible, key=str)

    chunks, model_id, collection = await _search_scoped_spaces(
        session,
        qdrant=qdrant,
        organization_id=org_id,
        space_ids=effective,
        query=query,
        top_k=top_k,
        document_ids=document_ids,
    )
    return ScopedRetrieval(
        chunks=chunks,
        space_ids=effective,
        scope_mode=scope_mode,
        model_id=model_id,
        collection=collection,
    )


async def _search_scoped_spaces(
    session: AsyncSession,
    *,
    qdrant: AsyncQdrantClient,
    organization_id: uuid.UUID,
    space_ids: list[uuid.UUID],
    query: str,
    top_k: int,
    document_ids: Sequence[uuid.UUID] | None = None,
) -> tuple[list[RetrievedChunk], str, str]:
    """Embed ``query`` and search ``space_ids``; return (chunks, model_id, collection).

    Shared by :func:`retrieve_for_conversation` and
    :func:`retrieve_for_user`. Never raises for "empty scope"/"no hits";
    only provider misconfiguration or Qdrant being down raises.
    """

    org_id = organization_id

    # Resolve the org's embedding provider (must match the one that
    # indexed the docs — unmatched vector spaces just produce no hits).
    resolved = await resolve_embedding_provider(session, organization_id=org_id)
    embedder: EmbeddingProvider = resolved.provider  # type: ignore[assignment]
    model_id = resolved.model.model_id
    vector_dim = resolved.model.vector_dim or embedder.vector_dim
    store = VectorStore(qdrant, model_id=model_id, vector_dim=vector_dim)

    if not space_ids:
        _log.info("retrieve: empty scope in %s", org_id)
        return [], model_id, store.collection

    # Embed query. BGE-M3 wants the whole query embedded like any other
    # document; instruction-aware providers consume the hint.
    vectors = await embedder.embed(
        [query],
        model=model_id,
        instruction="Given a user question, retrieve relevant passages.",
    )
    if not vectors:
        raise RuntimeError("embedding provider returned no vectors for query")
    query_vector = vectors[0]

    # Server-side filtered search. Over-fetch when a reranker is
    # configured so the cross-encoder has more candidates to reorder.
    reranker_model_id = await _selected_reranker(session, organization_id=org_id)
    over_fetch = max(top_k * 3, top_k) if reranker_model_id else top_k
    raw = await store.search(
        query_vector=query_vector,
        space_ids=space_ids,
        organization_id=org_id,
        limit=max(1, min(int(over_fetch), 40)),
        document_ids=document_ids,
    )

    # Defensive re-check — reject any hit that escaped the filter.
    accessible_set = set(space_ids)
    kept = [c for c in raw if c.space_id in accessible_set]
    if len(kept) != len(raw):
        _log.warning(
            "retrieve: dropped %d hit(s) that leaked past the space filter",
            len(raw) - len(kept),
        )

    # Optional rerank pass.
    if reranker_model_id and len(kept) > 1:
        from quberi_api.services.ai.local_reranker import LocalRerankerProvider

        rr = LocalRerankerProvider(model_id=reranker_model_id)
        try:
            scores = await rr.rerank(query=query, candidates=[c.text for c in kept])
            reordered = sorted(
                zip(kept, scores), key=lambda pair: pair[1], reverse=True
            )
            kept = [
                RetrievedChunk(
                    document_id=c.document_id,
                    space_id=c.space_id,
                    document_name=c.document_name,
                    chunk_index=c.chunk_index,
                    text=c.text,
                    score=float(s),
                )
                for c, s in reordered[:top_k]
            ]
            _log.info(
                "retrieve: reranked %d candidates → top %d (model=%s)",
                len(raw), len(kept), reranker_model_id,
            )
        except Exception:  # noqa: BLE001 — fall back to raw order
            _log.exception("reranker failed; using dense-only order")
            kept = kept[:top_k]
    else:
        kept = kept[:top_k]

    return kept, model_id, store.collection


async def _selected_reranker(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> str | None:
    """Return the org's reranker ``model_id`` if one is configured.

    No platform default — reranker is opt-in per org because the
    ~570 MB model is heavy for orgs that don't need it.
    """

    row = (
        await session.execute(
            select(AiModelSelection).where(
                AiModelSelection.organization_id == organization_id,
                AiModelSelection.purpose == "reranker",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    spec = MODEL_REGISTRY.get(row.model_id)
    if spec is None or spec.purpose != "reranker":
        return None
    return row.model_id


async def embed_query(
    session: AsyncSession, *, organization_id: uuid.UUID, query: str
) -> tuple[list[float], str, int]:
    """One-shot: resolve embedding provider and vectorise ``query``.

    Returned tuple is ``(vector, model_id, vector_dim)``. Useful for
    tests and future non-conversation search surfaces.
    """

    resolved = await resolve_embedding_provider(
        session, organization_id=organization_id
    )
    embedder: EmbeddingProvider = resolved.provider  # type: ignore[assignment]
    vectors = await embedder.embed([query], model=resolved.model.model_id)
    if not vectors:
        raise RuntimeError("empty embedding result")
    return (
        vectors[0],
        resolved.model.model_id,
        resolved.model.vector_dim or embedder.vector_dim,
    )
