"""Shared Redis connection settings for the arq queue.

Split out from :mod:`quberi_api.workers.rag` so :mod:`enqueue` can
import Redis settings without triggering the RAG import chain (which
transitively pulls in ``services.documents`` — which in turn imports
``workers.enqueue``, creating a cycle).
"""

from __future__ import annotations

from urllib.parse import urlparse

from arq.connections import RedisSettings

from quberi_api.core.config import get_settings


def build_redis_settings() -> RedisSettings:
    settings = get_settings()
    parsed = urlparse(settings.resolved_arq_redis_url())
    host = parsed.hostname or "redis"
    port = parsed.port or 6379
    db = 0
    if parsed.path and parsed.path.lstrip("/").isdigit():
        db = int(parsed.path.lstrip("/"))
    return RedisSettings(
        host=host,
        port=port,
        database=db,
        password=parsed.password,
    )
