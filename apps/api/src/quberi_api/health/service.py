import asyncio
import logging
from collections.abc import Awaitable, Callable
from time import perf_counter

from quberi_api.core.config import Settings
from quberi_api.health.schemas import ComponentHealth, ReadinessResponse
from quberi_api.infrastructure.postgres import probe_postgres
from quberi_api.infrastructure.registry import Infrastructure

logger = logging.getLogger(__name__)


async def _timed_probe(name: str, probe: Callable[[], Awaitable[object]]) -> ComponentHealth:
    started_at = perf_counter()
    try:
        await probe()
        status = "up"
    except Exception:
        logger.warning("Infrastructure health probe failed: %s", name, exc_info=True)
        status = "down"
    latency_ms = round((perf_counter() - started_at) * 1000, 2)
    return ComponentHealth(name=name, status=status, latency_ms=latency_ms)


async def get_readiness(
    settings: Settings,
    infrastructure: Infrastructure,
) -> ReadinessResponse:
    async def postgres_probe() -> None:
        await probe_postgres(settings)

    async def redis_probe() -> None:
        await asyncio.wait_for(
            infrastructure.redis.ping(),
            timeout=settings.health_timeout_seconds,
        )

    async def qdrant_probe() -> None:
        await asyncio.wait_for(
            infrastructure.qdrant.get_collections(),
            timeout=settings.health_timeout_seconds,
        )

    async def object_storage_probe() -> None:
        await asyncio.wait_for(
            asyncio.to_thread(infrastructure.object_storage.list_buckets),
            timeout=settings.health_timeout_seconds,
        )

    components = await asyncio.gather(
        _timed_probe("postgres", postgres_probe),
        _timed_probe("redis", redis_probe),
        _timed_probe("qdrant", qdrant_probe),
        _timed_probe("object_storage", object_storage_probe),
    )
    status = "ready" if all(component.status == "up" for component in components) else "unready"
    return ReadinessResponse(
        status=status,
        version=settings.app_version,
        components=list(components),
    )
