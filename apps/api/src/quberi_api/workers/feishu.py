"""arq jobs for Feishu integration (webhook-driven incremental sync).

``sync_feishu_source`` re-runs the reference-only folder sync for one
``DocumentSource``. It's idempotent — unchanged docs are skipped via
checksum — so a webhook can safely enqueue it on every file event
without piling up redundant work.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from qdrant_client import AsyncQdrantClient

from quberi_api.domain.documents.models import DocumentSource

_log = logging.getLogger(__name__)


async def sync_feishu_source(ctx: dict[str, Any] | None, source_id: str) -> dict:
    """Re-sync a Feishu document source by id. arq entry point."""

    if ctx is None:
        raise RuntimeError("sync_feishu_source needs an arq ctx")
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    qdrant: AsyncQdrantClient = ctx["qdrant"]

    sid = uuid.UUID(source_id)
    async with session_factory() as session:
        row = (
            await session.execute(
                select(DocumentSource).where(DocumentSource.id == sid)
            )
        ).scalar_one_or_none()
        if row is None or row.kind != "feishu":
            return {"source_id": source_id, "skipped": "not a feishu source"}
        config = row.config or {}
        folder_token = config.get("folder_token")
        target_space_id = config.get("target_space_id")
        if not folder_token or not target_space_id:
            return {"source_id": source_id, "skipped": "missing config"}

        from quberi_api.services.documents.connectors.feishu import (
            sync_folder_documents,
        )

        report = await sync_folder_documents(
            session,
            qdrant=qdrant,
            organization_id=row.organization_id,
            target_space_id=uuid.UUID(str(target_space_id)),
            folder_token=folder_token,
            source_id=row.id,
            actor_user_id=None,
        )
        # Stamp last_synced_at.
        await session.commit()
        async with session.begin():
            fresh = (
                await session.execute(
                    select(DocumentSource).where(DocumentSource.id == sid)
                )
            ).scalar_one()
            from datetime import datetime, timezone

            fresh.last_synced_at = datetime.now(timezone.utc)

    _log.info(
        "feishu source %s synced: indexed=%d skipped=%d failed=%d deleted=%d",
        source_id, report.indexed, report.skipped, report.failed, report.deleted,
    )
    return {
        "source_id": source_id,
        "indexed": report.indexed,
        "skipped": report.skipped,
        "failed": report.failed,
        "deleted": report.deleted,
    }
