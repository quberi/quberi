import asyncpg

from quberi_api.core.config import Settings


async def probe_postgres(settings: Settings) -> None:
    connection = await asyncpg.connect(
        settings.postgres_dsn.get_secret_value(),
        timeout=settings.health_timeout_seconds,
    )
    try:
        await connection.fetchval("SELECT 1")
    finally:
        await connection.close()
