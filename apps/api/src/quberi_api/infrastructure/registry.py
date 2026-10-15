from dataclasses import dataclass

from minio import Minio
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from quberi_api.core.config import Settings
from quberi_api.db.session import create_engine, create_sessionmaker, dispose_engine
from quberi_api.infrastructure.object_storage import create_object_storage_client
from quberi_api.infrastructure.qdrant import create_qdrant_client
from quberi_api.infrastructure.redis import create_redis_client


@dataclass
class Infrastructure:
    """Owns every long-lived client the process shares across requests.

    Instances are constructed once during application startup and torn down
    during shutdown. Request-scoped resources (like a SQLAlchemy ``AsyncSession``)
    are minted from the shared factories held here rather than being placed on
    this object.
    """

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    qdrant: AsyncQdrantClient
    object_storage: Minio

    @classmethod
    def create(cls, settings: Settings) -> "Infrastructure":
        engine = create_engine(settings.postgres_dsn.get_secret_value())
        return cls(
            engine=engine,
            session_factory=create_sessionmaker(engine),
            redis=create_redis_client(settings),
            qdrant=create_qdrant_client(settings),
            object_storage=create_object_storage_client(settings),
        )

    async def close(self) -> None:
        await self.redis.aclose()
        await self.qdrant.close()
        await dispose_engine(self.engine)
