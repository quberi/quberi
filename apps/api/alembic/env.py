"""Alembic environment for Quberi.

Runs the schema migrations against the same async engine the app uses, and
sources the DSN from the ``QUBERI_POSTGRES_DSN`` environment variable via the
project ``Settings`` object so we never diverge between runtime and DDL.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy.engine import Connection

import quberi_api.domain  # noqa: F401  — populate Base.metadata
from alembic import context
from quberi_api.core.config import get_settings
from quberi_api.db import Base, build_async_dsn, create_engine

# ---------------------------------------------------------------------------
# Alembic Config object
# ---------------------------------------------------------------------------
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the async database URL from application settings."""

    settings = get_settings()
    return build_async_dsn(settings.postgres_dsn.get_secret_value())


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout without a DB."""

    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live async engine."""

    engine = create_engine(_database_url())
    try:
        async with engine.begin() as conn:
            await conn.run_sync(_do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
