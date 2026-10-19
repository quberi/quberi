"""Migration 0034: frozen spaces, session version, revoked sessions."""
from __future__ import annotations

import os
import subprocess
import sys
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from quberi_api.db.session import build_async_dsn, create_engine
from quberi_api.domain.identity.models import User
from quberi_api.domain.spaces.models import Space
from quberi_api.services import onboarding
from tests.conftest import APPS_API_DIR, TEST_POSTGRES_DSN


async def _bootstrap(session):
    unique = uuid.uuid4().hex[:10]
    return await onboarding.bootstrap_organization(
        session,
        name=f"Migration Org {unique}",
        slug=f"migration-org-{unique}",
        owner_email=f"owner-{unique}@example.com",
        owner_display_name="Owner",
        tier="team",
    )


@pytest.mark.asyncio
async def test_space_status_accepts_frozen_and_keeps_old_values(db_session):
    result = await _bootstrap(db_session)
    space = await db_session.get(Space, result.personal_space_id)
    for status in ("frozen", "archived", "active"):
        space.status = status
        await db_session.flush()


@pytest.mark.asyncio
async def test_space_status_rejects_unknown_value(db_session):
    result = await _bootstrap(db_session)
    await db_session.execute(text("SAVEPOINT bad_status"))
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text("UPDATE spaces SET status = 'deleted' WHERE id = :id"),
            {"id": result.personal_space_id},
        )
    await db_session.execute(text("ROLLBACK TO SAVEPOINT bad_status"))


@pytest.mark.asyncio
async def test_new_user_has_session_version_zero(db_session):
    result = await _bootstrap(db_session)
    user = await db_session.get(User, result.user_id)
    assert user.session_version == 0
    row = await db_session.execute(
        text("SELECT session_version FROM users WHERE id = :id"), {"id": result.user_id}
    )
    assert row.scalar_one() == 0


@pytest.mark.asyncio
async def test_revoked_sessions_table_exists_with_expected_columns(db_session):
    rows = await db_session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'revoked_sessions'"
        )
    )
    assert {r[0] for r in rows} == {"token_hash", "user_id", "expires_at", "created_at"}


def _alembic(dsn: str, *args: str) -> None:
    env = dict(os.environ)
    env["QUBERI_POSTGRES_DSN"] = dsn
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(APPS_API_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


@pytest.mark.asyncio
async def test_upgrade_downgrade_round_trip_folds_frozen_into_archived():
    """Runs on a throwaway database so the shared test database keeps its head schema."""
    base, _, _ = TEST_POSTGRES_DSN.rpartition("/")
    name = f"quberi_mig_{uuid.uuid4().hex[:8]}"
    temp_dsn = f"{base}/{name}"
    admin = await asyncpg.connect(TEST_POSTGRES_DSN)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    engine = None
    try:
        _alembic(temp_dsn, "upgrade", "head")
        engine = create_engine(build_async_dsn(temp_dsn))
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            result = await _bootstrap(session)
            await session.commit()
            await session.execute(
                text("UPDATE spaces SET status = 'frozen' WHERE id = :id"),
                {"id": result.personal_space_id},
            )
            await session.commit()

        _alembic(temp_dsn, "downgrade", "0033")
        async with factory() as session:
            status = await session.execute(
                text("SELECT status FROM spaces WHERE id = :id"),
                {"id": result.personal_space_id},
            )
            assert status.scalar_one() == "archived"
            columns = await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'users' AND column_name = 'session_version'"
                )
            )
            assert columns.first() is None
            tables = await session.execute(text("SELECT to_regclass('revoked_sessions')"))
            assert tables.scalar_one() is None
            with pytest.raises(IntegrityError):
                await session.execute(
                    text("UPDATE spaces SET status = 'frozen' WHERE id = :id"),
                    {"id": result.personal_space_id},
                )
            await session.rollback()

        _alembic(temp_dsn, "upgrade", "head")
        async with factory() as session:
            await session.execute(
                text("UPDATE spaces SET status = 'frozen' WHERE id = :id"),
                {"id": result.personal_space_id},
            )
            await session.commit()
    finally:
        if engine is not None:
            await engine.dispose()
        admin = await asyncpg.connect(TEST_POSTGRES_DSN)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()
