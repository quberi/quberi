"""Load the picture model's text half at start-up, but only where someone will need it.

The first picture search after a restart otherwise waits ~35 s for the model to load,
which is longer than many clients wait. Loading it costs ~1 GB, so an instance that has
no searchable pictures (nobody uses picture search there) must not pay that: the warm-up
runs only when at least one picture is already indexed.

It runs in the background after the API has started answering; a failure is logged and
changes nothing (the model then loads on the first search, as before).
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.core.config import Settings
from quberi_api.domain.documents.models import Document
from quberi_api.services.ai.local_image_embedding import get_image_embedder

_log = logging.getLogger(__name__)


async def has_searchable_pictures(session: AsyncSession) -> bool:
    found = (
        await session.execute(
            select(Document.id)
            .where(
                func.lower(Document.mime_type).like("image/%"),
                Document.indexing_state == "indexed",
                Document.status != "archived",
            )
            .limit(1)
        )
    ).first()
    return found is not None


async def warm_picture_search(session_factory, settings: Settings) -> bool:
    """Warm the model if picture search is on and there are pictures. True when it ran."""

    if not settings.image_search_enabled:
        return False
    try:
        async with session_factory() as session:
            if not await has_searchable_pictures(session):
                return False
        await get_image_embedder(settings.default_image_embedding_model).embed_texts(["warm up"])
    except Exception:  # noqa: BLE001 — never stop the API for this
        _log.exception("picture search warm-up failed; the first search will load the model")
        return False
    _log.info("picture search warmed up (%s)", settings.default_image_embedding_model)
    return True
