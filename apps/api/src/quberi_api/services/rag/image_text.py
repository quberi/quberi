"""Make the text inside a picture searchable by words.

Picture search by meaning cannot see the letters in a drawing: a part number, a
dimension, a nameplate. This module reads them (see ``services/ai/ocr``) and files them in
the **text** collection as ordinary chunks of the same document, so every rule that
already limits who may see a document applies to them with no new code.

State lives in ``documents.attributes["image_text"]`` and nowhere else:

    {
      "status": "indexed" | "failed" | "skipped",
      "error": "...",                       # when failed or skipped
      "source_checksum": "<picture sha256>",
      "embedding_model": "bge-m3",
      "ocr": {"engine": ..., "model": ..., "version": ...,
              "lines": [{"text", "conf", "box"}], "dropped": 0, "notes": []},
    }

The recognised lines are kept so that changing the embedding model re-embeds them instead
of reading every picture again.
"""

from __future__ import annotations

import logging
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from qdrant_client import AsyncQdrantClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.core.config import get_settings
from quberi_api.domain.documents.models import Document
from quberi_api.domain.spaces.models import Space
from quberi_api.services.ai import resolve_embedding_provider
from quberi_api.services.ai.ocr import (
    OcrError,
    OcrImageError,
    OcrLine,
    OcrResult,
    OcrUnavailableError,
    get_ocr_provider,
)
from quberi_api.services.ai.ocr.models import ENGINE_NAME, ENGINE_VERSION, MODEL_NAME
from quberi_api.services.rag.chunk import chunk_text
from quberi_api.services.rag.codes import codes_in_lines
from quberi_api.services.rag.vector_store import ChunkPoint, VectorStore

_log = logging.getLogger(__name__)

IMAGE_TEXT_KEY = "image_text"
EMBED_BATCH_SIZE = 32

# Diameter signs in drawings arrive as four different characters, and so does what a
# person types. The block carries the text as read AND with all four unified to one, so a
# query written either way matches.
_DIAMETER_SIGNS = "Φϕ∅Øøϕ⌀"
_DIAMETER_UNIFIED = "Φ"


def normalise(text: str) -> str:
    """NFKC (full-width digits and letters become plain ones), whitespace squeezed."""

    return " ".join(unicodedata.normalize("NFKC", text).split())


def unify_diameter(text: str) -> str:
    return "".join(_DIAMETER_UNIFIED if ch in _DIAMETER_SIGNS else ch for ch in text)


def build_block(document_name: str, lines: list[str]) -> str:
    """The text that gets embedded for one picture.

    One fixed shape, because what is in the block is what can be found::

        文件名：{name}
        图中文字：{line}；{line}；...

    Lines that differ from their diameter-unified form are written both ways.
    """

    seen: set[str] = set()
    parts: list[str] = []
    for raw in lines:
        line = normalise(raw)
        if not line:
            continue
        for variant in (line, unify_diameter(line)):
            if variant not in seen:
                seen.add(variant)
                parts.append(variant)
    header = f"文件名：{normalise(document_name)}"
    if not parts:
        return header
    return header + "\n图中文字：" + "；".join(parts)


@dataclass(frozen=True)
class ImageTextOutcome:
    document_id: uuid.UUID
    status: str  # indexed | skipped | failed
    reason: str | None = None
    chunk_count: int = 0
    ocr_ran: bool = False


# ---------------------------------------------------------------------------
# State stored on the document
# ---------------------------------------------------------------------------


def _lines_to_json(lines: list[OcrLine]) -> list[dict[str, Any]]:
    return [
        {
            "text": ln.text,
            "conf": round(ln.confidence, 4),
            "box": [[round(x, 1), round(y, 1)] for x, y in ln.polygon],
        }
        for ln in lines
    ]


def _lines_from_json(rows: list[dict[str, Any]]) -> list[str]:
    return [str(r.get("text", "")) for r in rows]


def stored_state(doc: Document) -> dict[str, Any]:
    state = (doc.attributes or {}).get(IMAGE_TEXT_KEY)
    return state if isinstance(state, dict) else {}


def is_current(
    state: dict[str, Any], *, checksum: str | None, embedding_model: str
) -> bool:
    """Is the stored result still right for this picture, engine and embedding model?"""

    return (
        state.get("status") == "indexed"
        and state.get("source_checksum") == (checksum or "")
        and state.get("embedding_model") == embedding_model
        and (state.get("ocr") or {}).get("version") == ENGINE_VERSION
    )


async def _save_state(
    session: AsyncSession, document_id: uuid.UUID, state: dict[str, Any]
) -> None:
    """Write ``attributes["image_text"]`` without clobbering the other keys.

    ``attributes`` is rewritten as a whole, and the picture-embedding job writes
    ``image_index_model`` into the same column. So the row is locked and re-read first,
    and only our key is changed on the fresh copy.
    """

    fresh = (
        await session.execute(
            select(Document).where(Document.id == document_id).with_for_update()
        )
    ).scalar_one()
    fresh.attributes = {**(fresh.attributes or {}), IMAGE_TEXT_KEY: state}
    await session.commit()


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


async def _precondition(session: AsyncSession, doc: Document) -> str | None:
    """Why this picture must not be read now, or ``None`` when it may."""

    settings = get_settings()
    if not settings.image_ocr_enabled:
        return "OCR is turned off on this server"
    if doc.status == "archived":
        return "the document is archived"
    if doc.review_state != "approved":
        return "the document is not approved"
    if not doc.storage_key:
        return "the document has no stored file"
    space_status = (
        await session.execute(select(Space.status).where(Space.id == doc.space_id))
    ).scalar_one_or_none()
    if space_status != "active":
        return "the space is not active"
    return None


async def index_image_text(
    session: AsyncSession,
    *,
    qdrant: AsyncQdrantClient,
    document_id: uuid.UUID,
    blob_loader: Any,
    force: bool = False,
) -> ImageTextOutcome:
    """Read the text in one picture and index it.

    ``blob_loader`` is ``async (storage_key) -> bytes``; it is only called when the
    picture has to be read again. ``force`` ignores a result that is already current.
    """

    doc = (
        await session.execute(select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()
    if doc is None:
        return ImageTextOutcome(document_id, "skipped", "the document no longer exists")

    blocked = await _precondition(session, doc)
    if blocked is not None:
        # Whether an archived or unapproved document stays searchable is decided by the
        # same code as for any other document; a picture's text gets no rule of its own.
        return ImageTextOutcome(document_id, "skipped", blocked)

    resolved = await resolve_embedding_provider(session, organization_id=doc.organization_id)
    embedding_model = resolved.model.model_id
    state = stored_state(doc)
    checksum = doc.checksum_sha256 or ""
    if not force and is_current(state, checksum=checksum, embedding_model=embedding_model):
        return ImageTextOutcome(document_id, "skipped", "already up to date")

    # Reuse the saved reading when only the embedding model changed.
    ocr_ran = False
    reusable = (
        state.get("status") == "indexed"
        and state.get("source_checksum") == checksum
        and (state.get("ocr") or {}).get("version") == ENGINE_VERSION
    )
    if reusable and not force:
        ocr_state = state["ocr"]
        line_texts = _lines_from_json(ocr_state.get("lines", []))
    else:
        try:
            blob = await blob_loader(doc.storage_key)
            result = await get_ocr_provider().recognize(blob)
        except OcrImageError as exc:
            # The picture itself cannot be read: not worth retrying.
            await _save_state(
                session, doc.id,
                {"status": "failed", "error": f"ocr: {exc}", "source_checksum": checksum},
            )
            return ImageTextOutcome(document_id, "failed", f"ocr: {exc}")
        except OcrUnavailableError as exc:
            # Switched off, or the engine could not run: leave no state so it is retried.
            return ImageTextOutcome(document_id, "skipped", str(exc))
        except OcrError as exc:
            return ImageTextOutcome(document_id, "failed", f"ocr: {exc}")
        ocr_ran = True
        ocr_state = _ocr_state(result)
        line_texts = [ln.text for ln in result.lines]

    block = build_block(doc.name, line_texts)
    chunks = chunk_text(block)
    codes = tuple(codes_in_lines(line_texts))
    embedder = resolved.provider
    texts = [c.text for c in chunks]
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH_SIZE):
        vectors.extend(
            await embedder.embed(texts[start : start + EMBED_BATCH_SIZE], model=embedding_model)
        )
    store = VectorStore(
        qdrant,
        model_id=embedding_model,
        vector_dim=resolved.model.vector_dim or embedder.vector_dim,
    )
    await store.upsert_document(
        organization_id=doc.organization_id,
        space_id=doc.space_id,
        document_id=doc.id,
        document_name=doc.name,
        document_checksum=doc.checksum_sha256,
        chunks=[
            ChunkPoint(
                chunk_index=c.index,
                text=c.text,
                vector=v,
                char_start=c.char_start,
                char_end=c.char_end,
                source_kind="image_ocr",
                # Every chunk of the picture carries the keys, so whichever one is
                # returned can be found by them.
                codes=codes,
            )
            for c, v in zip(chunks, vectors, strict=True)
        ],
    )
    await _save_state(
        session,
        doc.id,
        {
            "status": "indexed",
            "source_checksum": checksum,
            "embedding_model": embedding_model,
            "ocr": ocr_state,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    _log.info(
        "indexed text of picture %s: %d line(s), %d chunk(s) (ocr_ran=%s)",
        doc.id, len(line_texts), len(chunks), ocr_ran,
    )
    return ImageTextOutcome(document_id, "indexed", None, len(chunks), ocr_ran)


async def index_image_text_job(ctx: dict[str, Any] | None, document_id: str) -> dict:
    """arq job entry: read the text in one picture."""

    if ctx is None:
        raise RuntimeError("index_image_text must be called with an arq ctx")
    from quberi_api.services.rag.indexer import _download_blob

    async with ctx["session_factory"]() as session:
        outcome = await index_image_text(
            session,
            qdrant=ctx["qdrant"],
            document_id=uuid.UUID(document_id),
            blob_loader=_download_blob,
        )
    return {
        "document_id": document_id,
        "status": outcome.status,
        "reason": outcome.reason,
        "chunk_count": outcome.chunk_count,
    }


def _ocr_state(result: OcrResult) -> dict[str, Any]:
    return {
        "engine": result.engine or ENGINE_NAME,
        "model": result.model or MODEL_NAME,
        "version": result.version,
        "width": result.width,
        "height": result.height,
        "lines": _lines_to_json(result.lines),
        "dropped": result.dropped,
        "notes": result.notes,
    }


__all__ = [
    "IMAGE_TEXT_KEY",
    "ImageTextOutcome",
    "build_block",
    "index_image_text",
    "index_image_text_job",
    "is_current",
    "normalise",
    "stored_state",
    "unify_diameter",
]
