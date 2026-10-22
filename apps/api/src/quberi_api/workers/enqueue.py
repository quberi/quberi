"""One-shot arq enqueue helpers for the API side.

The API doesn't hold a persistent arq pool — jobs are enqueued on
demand from request handlers. Opening a fresh Redis connection per
enqueue is cheap (~5ms) and avoids sharing state between requests.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from arq import create_pool

from quberi_api.workers.settings import build_redis_settings

_log = logging.getLogger(__name__)


async def enqueue_index_document(document_id: uuid.UUID) -> Optional[str]:
    """Enqueue an indexing job for ``document_id``.

    Returns the arq job id, or ``None`` if enqueue failed (Redis down,
    etc.). Failures are logged, never raised — an upload should still
    succeed even if the async pipeline is temporarily unreachable;
    the user can trigger a manual reindex.
    """

    try:
        pool = await create_pool(build_redis_settings())
    except Exception:  # noqa: BLE001
        _log.exception("could not open arq pool to enqueue %s", document_id)
        return None
    try:
        job = await pool.enqueue_job("index_document", str(document_id))
        return job.job_id if job else None
    except Exception:  # noqa: BLE001
        _log.exception("failed to enqueue index_document for %s", document_id)
        return None
    finally:
        await pool.close()


async def enqueue_image_text(document_id: uuid.UUID, checksum: Optional[str]) -> Optional[str]:
    """Enqueue reading the text in a picture.

    The job id carries the picture's checksum. arq keeps a finished job's result for an
    hour, and a job id it still remembers is refused, so a fixed id per document would
    swallow the request made when the picture is replaced within that hour and leave the
    old text searchable. A different file is a different id; the same file asked twice is
    queued once.
    """

    try:
        pool = await create_pool(build_redis_settings())
    except Exception:  # noqa: BLE001
        _log.exception("could not open arq pool to enqueue picture text for %s", document_id)
        return None
    try:
        job = await pool.enqueue_job(
            "index_image_text_job",
            str(document_id),
            _job_id=f"image-text-{document_id}-{(checksum or 'none')[:16]}",
        )
        return job.job_id if job else None
    except Exception:  # noqa: BLE001
        _log.exception("failed to enqueue index_image_text_job for %s", document_id)
        return None
    finally:
        await pool.close()


async def enqueue_image_caption(document_id: uuid.UUID, checksum: Optional[str]) -> Optional[str]:
    """Enqueue describing a picture with the organisation's vision model.

    The job id carries the picture's checksum for the same reason as
    :func:`enqueue_image_text`: a replaced picture is a different job.
    """

    try:
        pool = await create_pool(build_redis_settings())
    except Exception:  # noqa: BLE001
        _log.exception("could not open arq pool to enqueue picture description for %s", document_id)
        return None
    try:
        job = await pool.enqueue_job(
            "index_image_caption_job",
            str(document_id),
            _job_id=f"image-caption-{document_id}-{(checksum or 'none')[:16]}",
        )
        return job.job_id if job else None
    except Exception:  # noqa: BLE001
        _log.exception("failed to enqueue index_image_caption_job for %s", document_id)
        return None
    finally:
        await pool.close()


async def enqueue_feishu_sync(source_id: uuid.UUID) -> Optional[str]:
    """Enqueue an incremental re-sync of a Feishu document source.

    Fired by the webhook receiver on file events. Idempotent downstream
    (checksum-skip), so duplicate enqueues are harmless.
    """

    try:
        pool = await create_pool(build_redis_settings())
    except Exception:  # noqa: BLE001
        _log.exception("could not open arq pool to enqueue feishu sync %s", source_id)
        return None
    try:
        job = await pool.enqueue_job("sync_feishu_source", str(source_id))
        return job.job_id if job else None
    except Exception:  # noqa: BLE001
        _log.exception("failed to enqueue sync_feishu_source for %s", source_id)
        return None
    finally:
        await pool.close()
