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
from quberi_api.domain.enums import BLOCKED_USER_STATUSES, READABLE_SPACE_STATUSES
from quberi_api.domain.identity.models import User
from quberi_api.domain.spaces.models import Space

_log = logging.getLogger(__name__)


async def _pause_for_member(
    session: AsyncSession, source_id: uuid.UUID, member_id: uuid.UUID
) -> None:
    """Mark a source paused because the member who connected it is no longer active."""
    await session.rollback()  # end the read transaction before opening a write one
    async with session.begin():
        row = (
            await session.execute(
                select(DocumentSource)
                .where(DocumentSource.id == source_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is not None and row.status == "active":
            row.status = "paused"
            row.config = {
                **(row.config or {}),
                "paused_reason": "member_deactivated",
                "paused_member_id": str(member_id),
            }


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
        # Nothing below raises: a skipped job is a finished job for arq, so a source that
        # must not sync is not retried and does not fill the log with errors.
        if row.status != "active":
            _log.info("feishu source %s not synced: source not active", source_id)
            return {"source_id": source_id, "skipped": "source not active"}
        if row.created_by_user_id is not None:
            connector_status = (
                await session.execute(
                    select(User.status).where(User.id == row.created_by_user_id)
                )
            ).scalar_one_or_none()
            if connector_status in BLOCKED_USER_STATUSES:
                # A sync queued before the member was deactivated, or a source missed
                # when they were: stop it for good. A source whose connector row is gone
                # (created_by_user_id NULL) has nobody to judge and keeps syncing.
                await _pause_for_member(session, sid, row.created_by_user_id)
                _log.info("feishu source %s not synced: connector suspended", source_id)
                return {"source_id": source_id, "skipped": "connector suspended"}
        config = row.config or {}
        folder_token = config.get("folder_token")
        target_space_id = config.get("target_space_id")
        if not folder_token or not target_space_id:
            return {"source_id": source_id, "skipped": "missing config"}

        target_status = (
            await session.execute(
                select(Space.status).where(
                    Space.id == uuid.UUID(str(target_space_id)),
                    Space.organization_id == row.organization_id,
                )
            )
        ).scalar_one_or_none()
        if target_status not in READABLE_SPACE_STATUSES:
            # Gone, or frozen (a frozen space takes no writes, synced ones included).
            _log.info("feishu source %s not synced: target space unavailable", source_id)
            return {"source_id": source_id, "skipped": "target space unavailable"}
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
