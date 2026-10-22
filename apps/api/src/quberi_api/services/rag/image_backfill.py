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

:func:`pictures_needing_text` is the same idea for the text inside pictures: pictures that
were indexed before it could be read.
"""

from __future__ import annotations

import uuid

from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.documents.models import Document
from quberi_api.domain.identity.models import Organization
from quberi_api.domain.spaces.models import Space
from quberi_api.services.ai.ocr.models import ENGINE_VERSION
from quberi_api.services.rag.image_caption import IMAGE_CAPTION_KEY
from quberi_api.services.rag.image_text import IMAGE_TEXT_KEY
from quberi_api.services.rag.indexer import (
    IMAGE_INDEX_MODEL_KEY,
    LEGACY_IMAGE_MODEL,
    POSITIONS_FORMAT,
    POSITIONS_KEY,
)

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
            Document.review_state == "approved",
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


async def documents_needing_positions(
    session: AsyncSession, *, limit: int
) -> list[uuid.UUID]:
    """Indexed documents that were indexed before their passages recorded a place.

    Oldest first. A document with no stored file (a page pulled from Feishu), a picture, one
    that is not approved, archived, in an inactive space, or that failed to index is not
    listed: the first two have no place to record, the rest must not be touched, and a failure
    would only fail again. Whether to index it again is settled by one attribute, so each
    document is done once.
    """

    stmt = (
        select(Document.id)
        .join(Space, Space.id == Document.space_id)
        .where(
            Document.storage_key.is_not(None),
            ~func.lower(Document.mime_type).like("image/%"),
            Document.status != "archived",
            Document.review_state == "approved",
            Document.indexing_state == "indexed",
            Space.status == "active",
            func.coalesce(Document.attributes[POSITIONS_KEY].astext, "") != str(POSITIONS_FORMAT),
        )
        .order_by(Document.created_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def pictures_needing_caption(
    session: AsyncSession, *, limit: int
) -> list[tuple[uuid.UUID, str]]:
    """Pictures of organisations that turned descriptions on, not yet described.

    ``(id, checksum)`` pairs, oldest first. Only pictures that are indexed, approved, not
    archived, in an active space, a stored file and not a guest upload; a description that
    failed for the same file is not retried. A description made by an earlier model is kept
    (describing everything again for a change of model would cost a call per picture).
    """

    state = Document.attributes[IMAGE_CAPTION_KEY]
    stmt = (
        select(Document.id, Document.checksum_sha256)
        .join(Space, Space.id == Document.space_id)
        .join(Organization, Organization.id == Document.organization_id)
        .where(
            Organization.image_vision_enabled.is_(True),
            func.lower(Document.mime_type).like("image/%"),
            Document.status != "archived",
            Document.review_state == "approved",
            Document.storage_key.is_not(None),
            Document.checksum_sha256.is_not(None),
            Document.indexing_state == "indexed",
            Space.status == "active",
            Document.attributes["guest_owner"].astext.is_(None),
            or_(
                not_(Document.attributes.has_key(IMAGE_CAPTION_KEY)),
                state["source_checksum"].astext != Document.checksum_sha256,
            ),
        )
        .order_by(Document.created_at.asc())
        .limit(limit)
    )
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


async def pictures_needing_text(
    session: AsyncSession, *, limit: int
) -> list[tuple[uuid.UUID, str]]:
    """Pictures whose text has not been read, or was read from another file or engine.

    ``(id, checksum)`` pairs, oldest first. Only pictures that already have their picture
    vector are listed: one that has not is read right after it is indexed. Left alone:

    * a picture whose reading failed for a reason about the picture (``failed``) and is
      still the same file: retrying it would only fail again;
    * a reading that is current for the file and the engine.

    Whether the embedding model has changed since is not decided here (it is per
    organisation); the job itself notices and re-files the text.
    """

    state = Document.attributes[IMAGE_TEXT_KEY]
    stmt = (
        select(Document.id, Document.checksum_sha256)
        .join(Space, Space.id == Document.space_id)
        .where(
            func.lower(Document.mime_type).like("image/%"),
            Document.status != "archived",
            Document.review_state == "approved",
            Document.storage_key.is_not(None),
            Document.checksum_sha256.is_not(None),
            Document.indexing_state == "indexed",
            Space.status == "active",
            or_(
                not_(Document.attributes.has_key(IMAGE_TEXT_KEY)),
                # Read from another file than the one it is now.
                state["source_checksum"].astext != Document.checksum_sha256,
                # Read by an older engine. (A failure is not retried just because the engine
                # changed: it failed for a reason about the picture.)
                and_(
                    state["status"].astext == "indexed",
                    func.coalesce(state["ocr"]["version"].astext, "") != ENGINE_VERSION,
                ),
            ),
        )
        .order_by(Document.created_at.asc())
        .limit(limit)
    )
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]
