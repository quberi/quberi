"""Feishu Cloud Docs connector — real implementation (波 1).

Sync strategy: **reference-only** — we never mirror bytes to MinIO.

For every ``docx`` file inside the configured folder we:
1. Pull ``docx.raw_content`` (plain text) via the platform Feishu client
2. Chunk + embed via the standard RAG pipeline
3. Insert a ``documents`` row with ``storage_key=None``,
   ``caching_policy='reference_only'``, and
   ``external_ref={"doc_token": "..."}`` so future re-fetches can find
   the source
4. Upsert vectors into the Quberi Qdrant collection

At retrieval time, the RAG service uses ``external_ref.doc_token`` to
re-fetch the segment via Feishu if it needs the original text (H-2
callers already have the chunk text in Qdrant payload, but the doc
detail drawer / citations UI may want fresh content).

``config`` shape stored on the ``DocumentSource`` row::

    {
      "folder_token": "Nahbf...",
      "target_space_id": "<uuid of the Quberi space these docs land in>",
      "caching_policy": "reference_only"   # informative; enforced here
    }
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from qdrant_client import AsyncQdrantClient

from quberi_api.core.config import get_settings
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document
from quberi_api.infrastructure.qdrant import create_qdrant_client
from quberi_api.services.errors import InvalidInputError, ServiceError
from quberi_api.services.integrations.feishu import (
    FeishuError,
    FeishuNotConfiguredError,
    get_platform_client,
)
from quberi_api.services.integrations.feishu.docs import (
    get_document_text,
    list_files,
)

# ``services.rag.indexer`` transitively re-enters ``services.documents.*``
# via the ``connectors.local`` import chain, causing a circular import
# during module load. Deferred inside the functions below.

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Legacy connector protocol methods (kept because document_sources.py
# still fans out via ``connectors.feishu.test_connection`` / ``.sync``).
# ---------------------------------------------------------------------------


async def test_connection(config: dict, credentials: dict | None) -> None:
    """Probe the platform Feishu credentials + the folder is reachable.

    ``credentials`` is unused today — the platform ``QUBERI_FEISHU_*``
    env vars are the source of truth. Per-org BYO Feishu credentials
    land in a later slice.
    """

    _ = credentials
    folder_token = (config or {}).get("folder_token")
    if not folder_token:
        raise InvalidInputError(
            "feishu source config must include 'folder_token'"
        )
    try:
        client = await get_platform_client()
    except FeishuNotConfiguredError as exc:
        raise ServiceError(str(exc)) from exc
    try:
        files = await list_files(client, folder_token=folder_token, page_size=1)
    except FeishuError as exc:
        raise ServiceError(f"feishu list_files failed: {exc}") from exc

    if not files:
        _log.info(
            "feishu test_connection: folder %s reachable but empty",
            folder_token,
        )


async def sync(config: dict, credentials: dict | None) -> None:
    """Legacy protocol entry — funnels to :func:`sync_folder_documents`.

    ``document_sources.sync_source`` calls this signature. Since it
    doesn't hand us a session or actor, we open our own session +
    Qdrant client. Not the cleanest but keeps the connector protocol
    backwards-compatible for now.
    """

    _ = credentials
    folder_token = (config or {}).get("folder_token")
    target_space_id = (config or {}).get("target_space_id")
    organization_id = (config or {}).get("organization_id")
    if not (folder_token and target_space_id and organization_id):
        raise InvalidInputError(
            "feishu sync requires config with folder_token, target_space_id, "
            "organization_id (populated by document_sources.sync_source)"
        )

    from quberi_api.db.session import create_engine, create_sessionmaker

    settings = get_settings()
    engine = create_engine(settings.postgres_dsn.get_secret_value())
    session_factory = create_sessionmaker(engine)
    qdrant = create_qdrant_client(settings)
    try:
        async with session_factory() as session:
            await sync_folder_documents(
                session,
                qdrant=qdrant,
                organization_id=uuid.UUID(str(organization_id)),
                target_space_id=uuid.UUID(str(target_space_id)),
                folder_token=folder_token,
                source_id=(config or {}).get("_source_id"),
                actor_user_id=(config or {}).get("_actor_user_id"),
            )
    finally:
        await qdrant.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# The real work
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FolderSyncReport:
    """Summary of one ``sync_folder_documents`` run."""

    scanned: int  # files listed in folder (all types)
    indexed: int  # docs whose text landed in Qdrant
    skipped: int  # non-docx types we don't handle yet
    failed: int  # extraction / embedding failures
    deleted: int  # docs previously in this folder but no longer present


async def sync_folder_documents(
    session: AsyncSession,
    *,
    qdrant: AsyncQdrantClient,
    organization_id: uuid.UUID,
    target_space_id: uuid.UUID,
    folder_token: str,
    source_id: Optional[Any] = None,
    actor_user_id: Optional[Any] = None,
) -> FolderSyncReport:
    """Enumerate the Feishu folder, upsert every docx into Quberi.

    Reference-only: we store ``documents.storage_key=None`` and the
    Feishu ``doc_token`` in ``external_ref``. Deleting an entry that
    was previously synced but is now missing from the folder is
    handled at the end (soft delete + Qdrant purge).
    """

    client = await get_platform_client()

    # -- 1. Enumerate current folder state -----------------------------
    try:
        rows = await list_files(client, folder_token=folder_token)
    except FeishuError as exc:
        raise ServiceError(f"feishu list_files failed: {exc}") from exc

    docx_rows = [r for r in rows if r.type == "docx"]
    non_docx = [r for r in rows if r.type != "docx"]
    _log.info(
        "feishu sync %s: %d docx + %d other (skipped)",
        folder_token, len(docx_rows), len(non_docx),
    )

    # -- 2. Existing Quberi docs synced from this folder ---------------
    # We identify them by ``external_ref->>'folder_token'`` — cheap
    # to write, doesn't need a schema change.
    existing_stmt = select(Document).where(
        Document.organization_id == organization_id,
        Document.space_id == target_space_id,
        Document.external_ref["folder_token"].astext == folder_token,
    )
    existing_docs = (await session.execute(existing_stmt)).scalars().all()
    by_token = {
        (d.external_ref or {}).get("doc_token"): d for d in existing_docs
    }

    report = {"indexed": 0, "skipped": len(non_docx), "failed": 0}
    seen_tokens: set[str] = set()

    for row in docx_rows:
        seen_tokens.add(row.token)
        try:
            content = await get_document_text(client, document_id=row.token)
        except FeishuError as exc:
            _log.warning(
                "feishu sync: could not fetch %s (%s): %s",
                row.token, row.name, exc,
            )
            report["failed"] += 1
            continue

        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        doc = by_token.get(row.token)
        if doc is not None and doc.checksum_sha256 == checksum:
            # Content unchanged since last sync — no need to re-embed.
            # Still backfill the source link for rows created before the
            # source_id fix, so the origin badge corrects itself on the
            # next sync without a re-embed.
            if doc.source_id is None and source_id is not None:
                await session.commit()
                async with session.begin():
                    doc.source_id = _coerce_uuid(source_id)
            continue

        if doc is None:
            doc = Document(
                id=uuid.uuid4(),
                organization_id=organization_id,
                space_id=target_space_id,
                # Link to the Feishu DocumentSource so the UI shows the
                # correct "飞书" origin badge (``_to_view`` resolves the
                # source kind from this FK).
                source_id=_coerce_uuid(source_id),
                uploaded_by_user_id=_coerce_uuid(actor_user_id),
                name=row.name,
                mime_type="application/vnd.feishu.docx",
                size_bytes=len(content.encode("utf-8")),
                storage_key=None,  # ← reference-only, no MinIO blob
                checksum_sha256=checksum,
                external_ref={
                    "connector": "feishu",
                    "doc_token": row.token,
                    "folder_token": folder_token,
                    "url": row.url,
                    "source_id": str(source_id) if source_id else None,
                },
                sync_state="synced_fresh",
                status="uploaded",
                attributes={},
            )
            # Fresh row → single-txn insert
            await session.commit()
            async with session.begin():
                session.add(doc)
                session.add(
                    AuditEvent(
                        organization_id=organization_id,
                        actor_type=(
                            "user" if _coerce_uuid(actor_user_id) else "system"
                        ),
                        actor_id=_coerce_uuid(actor_user_id),
                        action="document.synced_from_feishu",
                        resource_type="document",
                        resource_id=doc.id,
                        space_id=target_space_id,
                        payload={
                            "doc_token": row.token,
                            "folder_token": folder_token,
                            "name": row.name,
                        },
                    )
                )
                await session.flush()
                await session.refresh(
                    doc, attribute_names=["created_at", "updated_at"]
                )
        else:
            # Existing row — content changed, update fields in place.
            doc.name = row.name
            doc.size_bytes = len(content.encode("utf-8"))
            doc.checksum_sha256 = checksum
            doc.sync_state = "synced_fresh"
            # Backfill the source link for rows created before this fix.
            if doc.source_id is None:
                doc.source_id = _coerce_uuid(source_id)
            await session.commit()

        # -- Index the text through the shared RAG pipeline -----------
        try:
            from quberi_api.services.rag.indexer import (
                index_document_from_text,
            )

            await index_document_from_text(
                session, qdrant=qdrant, document_id=doc.id, text=content
            )
            report["indexed"] += 1
        except Exception:  # noqa: BLE001
            _log.exception("feishu sync: indexing %s failed", doc.id)
            report["failed"] += 1

    # -- 3. Detect + soft-delete stale rows ----------------------------
    deleted = 0
    resolved_embedding = None
    for token, doc in by_token.items():
        if token in seen_tokens:
            continue
        _log.info(
            "feishu sync: doc %s (%s) no longer in folder — soft-deleting",
            doc.id, doc.name,
        )
        # Prune vectors first (best-effort).
        if resolved_embedding is None:
            try:
                from quberi_api.services.ai import resolve_embedding_provider

                resolved_embedding = await resolve_embedding_provider(
                    session, organization_id=organization_id
                )
            except Exception:  # noqa: BLE001
                resolved_embedding = False  # sentinel
        if resolved_embedding and resolved_embedding is not False:
            from quberi_api.services.rag.indexer import (
                remove_document_from_index,
            )

            await remove_document_from_index(
                qdrant=qdrant,
                document_id=doc.id,
                model_id=resolved_embedding.model.model_id,
                vector_dim=resolved_embedding.model.vector_dim or 1024,
            )
        # Soft-delete: mark archived (row survives for audit).
        doc.status = "archived"
        doc.sync_state = "synced_broken"
        deleted += 1
        await session.commit()

    return FolderSyncReport(
        scanned=len(rows),
        indexed=report["indexed"],
        skipped=report["skipped"],
        failed=report["failed"],
        deleted=deleted,
    )


def _coerce_uuid(v: Any) -> Optional[uuid.UUID]:
    if v is None:
        return None
    if isinstance(v, uuid.UUID):
        return v
    try:
        return uuid.UUID(str(v))
    except (ValueError, TypeError):
        return None
