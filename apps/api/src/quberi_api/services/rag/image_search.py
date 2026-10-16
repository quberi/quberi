"""Find pictures by a picture, by words, or both, inside the spaces a person can read.

The two steps that keep spaces apart:

1. :func:`resolve_image_scope` works out *where* to look. It starts from the spaces
   the person can read **right now** (recomputed from live permissions, never from
   anything the client sent) and, if they asked for particular spaces, keeps only
   those that are also readable: the intersection. Asking for a space you cannot
   read adds nothing; asking for none means all you can read.
2. :func:`search_images` looks only there, then checks **every hit against the
   database** before returning it: the document must still exist, be in the same
   space the vector says, in this organisation, not archived, indexed, and the
   space itself still active. A vector that is stale (the document was deleted or
   archived, the space archived) is dropped here even if removing it from the
   vector store failed.

The picture used as the query is decoded in memory and turned into a vector; it is
never written anywhere and never logged.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.core.config import get_settings
from quberi_api.domain.documents.models import Document
from quberi_api.domain.spaces.models import Space
from quberi_api.services.ai.local_image_embedding import BGE_VL_DIM, get_image_embedder
from quberi_api.services.authz import list_accessible_space_ids
from quberi_api.services.rag.image_store import ImageVectorStore

_log = logging.getLogger(__name__)

DEFAULT_TOP_K = 12
MAX_TOP_K = 30
# Ask the vector store for more than we return, so hits dropped by the database
# check do not leave the list short.
_OVERFETCH = 3
MAX_QUERY_TEXT_CHARS = 500


class ImageSearchError(Exception):
    """A request that cannot be answered, with a message safe to show."""


class ImageSearchDisabled(ImageSearchError):
    """Picture search is switched off on this server."""


@dataclass(frozen=True)
class ImageMatch:
    document_id: uuid.UUID
    document_name: str
    space_id: uuid.UUID
    space_name: str
    mime_type: str
    size_bytes: int
    score: float


@dataclass(frozen=True)
class ImageSearchResult:
    matches: list[ImageMatch]
    # The spaces that were actually searched (after the intersection).
    space_ids: list[uuid.UUID]


async def resolve_image_scope(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    caller_user_id: uuid.UUID,
    requested_space_ids: Iterable[uuid.UUID] | None = None,
) -> list[uuid.UUID]:
    """The spaces to search: what the person can read now, narrowed to what they asked for.

    ``requested_space_ids`` of ``None`` means "no preference": every readable space.
    An empty collection means "none": nothing is searched.
    """

    readable = await list_accessible_space_ids(
        session, user_id=caller_user_id, organization_id=organization_id
    )
    if requested_space_ids is None:
        return sorted(readable, key=str)
    wanted = set(requested_space_ids)
    return sorted((s for s in readable if s in wanted), key=str)


def _combine(vectors: Sequence[list[float]]) -> list[float]:
    """Add vectors and scale to unit length (an image plus a sentence about it)."""

    total = [sum(parts) for parts in zip(*vectors, strict=True)]
    norm = math.sqrt(sum(x * x for x in total))
    if norm == 0:
        raise ImageSearchError("the query could not be turned into a search")
    return [x / norm for x in total]


async def embed_query(*, image: bytes | None, text: str | None) -> list[float]:
    """One query vector from a picture, a sentence, or both. Nothing is stored."""

    settings = get_settings()
    if not settings.image_search_enabled:
        raise ImageSearchDisabled("picture search is turned off on this server")
    sentence = (text or "").strip()
    if len(sentence) > MAX_QUERY_TEXT_CHARS:
        raise ImageSearchError(f"the description is longer than {MAX_QUERY_TEXT_CHARS} characters")
    if image is None and not sentence:
        raise ImageSearchError("give a picture, a description, or both")
    embedder = get_image_embedder(settings.default_image_embedding_model)
    parts: list[list[float]] = []
    if image is not None:
        parts.append((await embedder.embed_images([image]))[0])
    if sentence:
        parts.append((await embedder.embed_texts([sentence]))[0])
    return parts[0] if len(parts) == 1 else _combine(parts)


async def search_images(
    session: AsyncSession,
    *,
    qdrant: AsyncQdrantClient,
    organization_id: uuid.UUID,
    space_ids: Sequence[uuid.UUID],
    query_vector: list[float],
    top_k: int = DEFAULT_TOP_K,
) -> ImageSearchResult:
    """Nearest pictures in ``space_ids``, each confirmed against the database."""

    top = max(1, min(int(top_k), MAX_TOP_K))
    searched = list(space_ids)
    if not searched:
        return ImageSearchResult(matches=[], space_ids=[])
    store = ImageVectorStore(
        qdrant, model_id=get_settings().default_image_embedding_model, vector_dim=BGE_VL_DIM
    )
    hits = await store.search_images(
        query_vector=query_vector,
        space_ids=searched,
        organization_id=organization_id,
        limit=top * _OVERFETCH,
    )
    if not hits:
        return ImageSearchResult(matches=[], space_ids=searched)

    rows = (
        await session.execute(
            select(Document, Space.name)
            .join(Space, Space.id == Document.space_id)
            .where(
                Document.id.in_({h.document_id for h in hits}),
                Document.organization_id == organization_id,
                Document.space_id.in_(searched),
                Document.status != "archived",
                Document.indexing_state == "indexed",
                Space.organization_id == organization_id,
                Space.status == "active",
            )
        )
    ).all()
    by_id = {doc.id: (doc, space_name) for doc, space_name in rows}

    matches: list[ImageMatch] = []
    for hit in hits:
        found = by_id.get(hit.document_id)
        if found is None:
            continue  # stale: deleted, archived, moved, or its space is gone
        doc, space_name = found
        if doc.space_id != hit.space_id:
            continue  # the vector names a different space than the document is in now
        matches.append(
            ImageMatch(
                document_id=doc.id,
                document_name=doc.name,
                space_id=doc.space_id,
                space_name=space_name,
                mime_type=doc.mime_type,
                size_bytes=int(doc.size_bytes or 0),
                score=hit.score,
            )
        )
        if len(matches) == top:
            break
    return ImageSearchResult(matches=matches, space_ids=searched)
