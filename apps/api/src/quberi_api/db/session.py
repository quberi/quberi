"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Schemes we accept from configuration; we normalise them to the asyncpg driver.
_SYNC_SCHEMES = ("postgresql://", "postgres://")
_ASYNC_SCHEME = "postgresql+asyncpg://"


def build_async_dsn(dsn: str) -> str:
    """Normalise a Postgres DSN so SQLAlchemy uses the asyncpg driver.

    Accepts ``postgresql://`` and ``postgres://`` for operator convenience while
    still speaking async under the hood. Any DSN that already targets a specific
    driver (``postgresql+asyncpg://``, ``postgresql+psycopg://`` ...) is returned
    unchanged so operators can override the driver explicitly if they need to.
    """

    if dsn.startswith("postgresql+"):
        return dsn
    for scheme in _SYNC_SCHEMES:
        if dsn.startswith(scheme):
            return _ASYNC_SCHEME + dsn[len(scheme) :]
    return dsn


def create_engine(dsn: str, *, echo: bool = False) -> AsyncEngine:
    """Create the shared async engine for the application process."""

    return create_async_engine(
        build_async_dsn(dsn),
        echo=echo,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an ``async_sessionmaker`` bound to the given engine."""

    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )


async def dispose_engine(engine: AsyncEngine) -> None:
    """Close all pooled connections owned by the engine."""

    await engine.dispose()
