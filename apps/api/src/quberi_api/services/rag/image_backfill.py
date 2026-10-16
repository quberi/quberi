"""Index pictures that were uploaded before picture search existed.

Until now a picture could not be indexed: it failed at text extraction with
``extract: no extractor …``. Those rows (and any picture still ``not_indexed``)
are picked up here and queued for the normal indexing job, which now knows what
to do with a picture. A picture that fails for a *picture* reason (a corrupt file,
an unsupported format) carries an ``image: …`` error instead and is left alone, so
nothing is retried forever.
"""

from __future__ import annotations

import uuid

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.documents.models import Document
from quberi_api.domain.spaces.models import Space

DEFAULT_BATCH = 200


async def pictures_needing_index(
    session: AsyncSession, *, limit: int = DEFAULT_BATCH
) -> list[uuid.UUID]:
    """Ids of pictures that have no picture vector yet, oldest first."""

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
                    Document.indexing_error.like("extract:%"),
                ),
            ),
        )
        .order_by(Document.created_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())
