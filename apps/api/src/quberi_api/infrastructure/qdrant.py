from qdrant_client import AsyncQdrantClient

from quberi_api.core.config import Settings


def create_qdrant_client(settings: Settings) -> AsyncQdrantClient:
    return AsyncQdrantClient(
        url=settings.qdrant_url,
        timeout=settings.health_timeout_seconds,
        check_compatibility=False,
    )
