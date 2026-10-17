"""Qdrant façade for picture vectors.

Pictures live in their own collection, apart from the text chunks::

    quberi_images__{sanitised_model_id}     e.g. quberi_images__siglip2_base

They cannot share one: the vectors have a different size and come from a
different model. Everything else follows the text store exactly, on purpose, so
the same isolation holds. One point per picture, carrying::

    {
      "organization_id": "…",   # tenant
      "space_id":        "…",   # the space the picture is filed in
      "document_id":     "…",
      "document_name":   "…",
      "mime_type":       "image/png",
      "document_checksum": "…",
    }

**Isolation.** A search is always filtered inside Qdrant by the caller's
organisation and by an explicit set of space ids, and an empty set returns
nothing rather than "everything". The caller builds that set from the person's
live permissions at the moment of the search and then checks every hit against
the database again (see :mod:`quberi_api.services.rag.image_search`); this
class only guarantees it never searches outside the set it is given.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from qdrant_client.http import models as qmodels

from quberi_api.services.rag.vector_store import VectorStore, _point_id_for

_log = logging.getLogger(__name__)

IMAGE_COLLECTION_PREFIX = "quberi_images__"


@dataclass(frozen=True)
class ImageHit:
    """A picture found by :meth:`ImageVectorStore.search`."""

    document_id: uuid.UUID
    space_id: uuid.UUID
    document_name: str
    mime_type: str
    score: float


class ImageVectorStore(VectorStore):
    """One vector per picture, in a collection of its own."""

    collection_prefix = IMAGE_COLLECTION_PREFIX

    async def upsert_image(
        self,
        *,
        organization_id: uuid.UUID,
        space_id: uuid.UUID,
        document_id: uuid.UUID,
        document_name: str,
        mime_type: str,
        document_checksum: str | None,
        vector: list[float],
    ) -> None:
        """Store (or replace) the picture's vector."""

        await self.ensure_collection()
        await self.delete_document(document_id=document_id)
        await self._client.upsert(
            collection_name=self._collection,
            points=[
                qmodels.PointStruct(
                    id=_point_id_for(document_id, 0),
                    vector=vector,
                    payload={
                        "organization_id": str(organization_id),
                        "space_id": str(space_id),
                        "document_id": str(document_id),
                        "document_name": document_name,
                        "mime_type": mime_type,
                        "document_checksum": document_checksum or "",
                    },
                )
            ],
            wait=True,
        )

    async def search_images(
        self,
        *,
        query_vector: list[float],
        space_ids: Iterable[uuid.UUID],
        organization_id: uuid.UUID,
        limit: int = 12,
        granted_document_ids: Iterable[uuid.UUID] | None = None,
        denied_document_ids: Iterable[uuid.UUID] | None = None,
    ) -> list[ImageHit]:
        """Nearest pictures inside the given organisation and spaces only.

        An empty ``space_ids`` returns nothing: it never means "no filter".
        ``granted_document_ids`` are searched in addition (pictures shared on their own
        from spaces the person cannot read as a whole); ``denied_document_ids`` are
        always excluded. Both are applied inside the index.
        """

        space_id_list = [str(s) for s in space_ids]
        granted_list = [str(d) for d in (granted_document_ids or [])]
        if not space_id_list and not granted_list:
            return []
        if not await self._client.collection_exists(self._collection):
            return []
        reachable: list[Any] = []
        if space_id_list:
            reachable.append(
                qmodels.FieldCondition(key="space_id", match=qmodels.MatchAny(any=space_id_list))
            )
        if granted_list:
            reachable.append(
                qmodels.FieldCondition(key="document_id", match=qmodels.MatchAny(any=granted_list))
            )
        denied_list = [str(d) for d in (denied_document_ids or [])]
        must_not: list[Any] = (
            [qmodels.FieldCondition(key="document_id", match=qmodels.MatchAny(any=denied_list))]
            if denied_list
            else []
        )
        response = await self._client.query_points(
            collection_name=self._collection,
            query=query_vector,
            query_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="organization_id",
                        match=qmodels.MatchValue(value=str(organization_id)),
                    ),
                    qmodels.Filter(should=reachable),
                ],
                must_not=must_not or None,
            ),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        points: Any = getattr(response, "points", None)
        if points is None:
            points = response
        hits: list[ImageHit] = []
        for point in points:
            payload = point.payload or {}
            try:
                hits.append(
                    ImageHit(
                        document_id=uuid.UUID(payload["document_id"]),
                        space_id=uuid.UUID(payload["space_id"]),
                        document_name=str(payload.get("document_name", "")),
                        mime_type=str(payload.get("mime_type", "")),
                        score=float(point.score or 0.0),
                    )
                )
            except (KeyError, ValueError):  # pragma: no cover — defensive
                _log.warning("skipping malformed image payload: %s", payload)
        return hits
