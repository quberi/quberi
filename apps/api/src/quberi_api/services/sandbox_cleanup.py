"""Deleting guest-sandbox files - on request and when they expire.

Both paths end up in :func:`delete_sandbox_document`, so the guarantee is the
same: when it returns, the database row, the stored file **and the vectors** are
gone, and the vectors were *checked*, not assumed. The generic document-delete
service swallows vector-store failures (it logs nothing the caller can act on),
which for an anonymous, publicly visible area would let a "deleted" file's text
be quoted back to a stranger. Here a vector purge that cannot be confirmed is
logged loudly and reported to the caller.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.core.config import get_settings
from quberi_api.domain.documents.models import Document
from quberi_api.infrastructure.qdrant import create_qdrant_client
from quberi_api.services import guest_sandbox
from quberi_api.services.ai import resolve_embedding_provider
from quberi_api.services.documents import core as documents_core

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeleteOutcome:
    document_id: uuid.UUID
    row_deleted: bool
    vectors_verified_gone: bool


async def delete_sandbox_document(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    space_id: uuid.UUID,
    org_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> DeleteOutcome:
    """Delete one sandbox document completely, verifying the vectors.

    Vectors are purged **before** the database row goes. If the process dies
    between the two steps the row still exists, so the next sweep finds the file
    again and finishes the job; the opposite order could leave vectors with no
    row pointing at them, which nothing would ever clean up.
    """

    # Which embedding collection holds this org's vectors.
    resolved = await resolve_embedding_provider(session, organization_id=org_id)
    model_id = resolved.model.model_id
    vector_dim = resolved.model.vector_dim or 1024
    await session.commit()

    qdrant = create_qdrant_client(get_settings())
    try:
        purge = await guest_sandbox.purge_vectors(
            qdrant=qdrant,
            document_id=document_id,
            model_id=model_id,
            vector_dim=vector_dim,
            image_model_id=get_settings().default_image_embedding_model,
        )
    finally:
        await qdrant.close()

    if purge.vectors_left != 0:
        # Keep the row so the next sweep retries; never claim success.
        _log.error(
            "sandbox: %d vector(s) of document %s could not be confirmed deleted; "
            "keeping the row so it is retried",
            purge.vectors_left,
            document_id,
        )
        return DeleteOutcome(document_id, row_deleted=False, vectors_verified_gone=False)

    await documents_core.delete_document(
        session,
        document_id=document_id,
        organization_id=org_id,
        space_id=space_id,
        caller_user_id=actor_user_id,
    )
    return DeleteOutcome(document_id, row_deleted=True, vectors_verified_gone=True)


async def sweep_expired(
    session: AsyncSession, *, org_id: uuid.UUID, space_id: uuid.UUID, actor_user_id: uuid.UUID
) -> int:
    """Delete every expired sandbox document. Returns how many were removed."""

    rows = (
        (await session.execute(select(Document).where(Document.space_id == space_id)))
        .scalars()
        .all()
    )
    expired = [d.id for d in rows if guest_sandbox.is_expired(d.attributes)]
    await session.commit()
    removed = 0
    for doc_id in expired:
        try:
            outcome = await delete_sandbox_document(
                session,
                document_id=doc_id,
                space_id=space_id,
                org_id=org_id,
                actor_user_id=actor_user_id,
            )
            removed += int(outcome.row_deleted)
        except Exception:  # noqa: BLE001 - one bad file must not stop the sweep
            _log.exception("sandbox: failed to delete expired document %s", doc_id)
            await session.rollback()
    if expired:
        _log.info("sandbox sweep: %d expired, %d removed", len(expired), removed)
    return removed
