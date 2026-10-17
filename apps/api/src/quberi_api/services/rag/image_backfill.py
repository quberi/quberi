"""Index the pictures that have no vector under the current picture model.

Three kinds of picture are picked up and queued for the normal indexing job:

* pictures uploaded before picture search existed (``not_indexed``, or failed at text
  extraction with ``extract: no extractor …`` back when a picture could not be indexed);
* pictures whose last attempt failed for a reason that is not about the picture itself
  (the model could not load, storage or the vector store was down): worth retrying;
* pictures indexed under a **different picture model** than the one now in use. After
  a change of model their old vectors are in another collection and invisible to
  search until they are re-indexed here.

A picture that fails for a *picture* reason (a corrupt file, an unsupported format)
carries an ``image: …`` error and is left alone, so nothing is retried forever.
"""

from __future__ import annotations

import uuid

from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.documents.models import Document
from quberi_api.domain.spaces.models import Space
from quberi_api.services.rag.indexer import IMAGE_INDEX_MODEL_KEY, LEGACY_IMAGE_MODEL

DEFAULT_BATCH = 200


async def pictures_needing_index(
    session: AsyncSession, *, model_id: str, limit: int = DEFAULT_BATCH
) -> list[uuid.UUID]:
    """Ids of pictures that have no vector under ``model_id`` yet, oldest first."""

    recorded_model = func.coalesce(
        Document.attributes[IMAGE_INDEX_MODEL_KEY].astext, LEGACY_IMAGE_MODEL
    )
    stmt = (
        select(Document.id)
        .join(Space, Space.id == Document.space_id)
        .where(
            func.lower(Document.mime_type).like("image/%"),
            Document.status != "archived",
            Space.status == "active",
            or_(
                Document.indexing_state == "not_indexed",
                and_(
                    Document.indexing_state == "failed",
                    not_(func.coalesce(Document.indexing_error, "").like("image:%")),
                ),
                and_(
                    Document.indexing_state == "indexed",
                    recorded_model != model_id,
                ),
            ),
        )
        .order_by(Document.created_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())
