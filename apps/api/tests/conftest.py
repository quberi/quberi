"""Shared pytest fixtures for the API test suite.

Strategy
--------
* A dedicated ``quberi_test`` Postgres database (never the dev database) is
  migrated to ``head`` **once per test session** via a subprocess call to
  Alembic — that keeps ``get_settings()``'s ``lru_cache`` (which reads env
  vars at first access) from ever seeing two different DSNs in one process.
* Every individual test runs inside a SAVEPOINT that is rolled back at
  teardown, so tests never leak rows into each other and never need manual
  cleanup — this is the standard SQLAlchemy "join an external transaction"
  pattern (see the pattern documented in the SQLAlchemy docs for isolating
  ORM tests within a session-per-test transaction).
* Infrastructure the domain layer doesn't need for pure service-level tests
  (Qdrant, MinIO, Redis) is NOT started here; add fixtures for those only
  when a test actually needs them.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Test database bootstrap
# ---------------------------------------------------------------------------

APPS_API_DIR = Path(__file__).resolve().parents[1]

# Never point this at the dev database — tests truncate/rollback aggressively
# via the outer-transaction pattern below, and a stray commit would corrupt
# real data. Override via TEST_POSTGRES_DSN if your local Postgres uses
# different credentials/port.
TEST_POSTGRES_DSN = os.environ.get(
    "TEST_POSTGRES_DSN",
    "postgresql://quberi:change-me-local-only@127.0.0.1:5432/quberi_test",
)

if "quberi_test" not in TEST_POSTGRES_DSN and "test" not in TEST_POSTGRES_DSN:
    raise RuntimeError(
        "refusing to run tests against a DSN that doesn't look like a test "
        f"database: {TEST_POSTGRES_DSN!r}. Set TEST_POSTGRES_DSN explicitly."
    )

# The encryption key only needs to be *some* valid Fernet key for tests;
# reuse the one from .env when present, else mint a throwaway one so
# ``seal``/``unseal`` (used by provider + agent credential storage) work
# without requiring a real .env file to exist.


def _fallback_encryption_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


os.environ.setdefault("QUBERI_POSTGRES_DSN", TEST_POSTGRES_DSN)
os.environ.setdefault("QUBERI_REDIS_URL", "redis://127.0.0.1:6379/1")
os.environ.setdefault("QUBERI_QDRANT_URL", "http://127.0.0.1:6333")
os.environ.setdefault("QUBERI_MINIO_ENDPOINT", "127.0.0.1:9000")
os.environ.setdefault("QUBERI_MINIO_ACCESS_KEY", "quberi-local")
os.environ.setdefault("QUBERI_MINIO_SECRET_KEY", "test-only")
os.environ.setdefault("QUBERI_ENVIRONMENT", "test")
os.environ.setdefault("QUBERI_ENCRYPTION_KEY", _fallback_encryption_key())


@pytest.fixture(scope="session", autouse=True)
def _migrate_test_database() -> None:
    """Run ``alembic upgrade head`` against the test DSN, once per session.

    Runs as a *subprocess* (not an in-process Alembic call) so it gets a
    clean environment and never shares SQLAlchemy engine/connection state
    with the test session fixtures below.
    """

    env = dict(os.environ)
    env["QUBERI_POSTGRES_DSN"] = TEST_POSTGRES_DSN
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(APPS_API_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "failed to migrate the test database "
            f"({TEST_POSTGRES_DSN}):\n{result.stdout}\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# Per-test session (outer transaction, rolled back on teardown)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session(_migrate_test_database: None) -> AsyncIterator[AsyncSession]:
    """A session bound to a single connection + outer transaction.

    Service code freely calls ``session.begin()`` / ``session.commit()``
    internally (that's the pattern used throughout ``quberi_api.services``).
    Nesting those inside an already-open *connection-level* transaction and
    joining new sessions to that same connection via ``bind=connection``
    means every commit the service layer issues actually lands on a
    SAVEPOINT, and the single outer ``trans.rollback()`` at teardown erases
    all of it — regardless of how many nested commits happened in between.
    """

    from quberi_api.db.session import build_async_dsn, create_engine

    engine = create_engine(build_async_dsn(TEST_POSTGRES_DSN))
    connection = await engine.connect()
    trans = await connection.begin()

    session_factory = async_sessionmaker(
        bind=connection, expire_on_commit=False, autoflush=False
    )
    session = session_factory()

    # Start a SAVEPOINT so that ``session.commit()`` calls made by service
    # code (which run inside ``session.begin()`` blocks) don't end the
    # outer transaction — SQLAlchemy nests automatically when a session is
    # joined to a connection that already has a transaction open.
    await connection.begin_nested()

    @__import__("sqlalchemy").event.listens_for(session.sync_session, "after_transaction_end")
    def _restart_savepoint(sess, transaction):  # noqa: ANN001
        if (
            transaction.nested
            and not transaction._parent.nested
            and connection.in_transaction()
        ):
            connection.sync_connection.begin_nested()

    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await connection.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# Domain fixtures — a ready-to-use organisation + owner user
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def org_and_owner(db_session: AsyncSession):
    """Bootstrap a fresh organisation with an owner user for one test.

    Uses the real ``onboarding.bootstrap_organization`` service so tests
    exercise the same seeding path production onboarding does (system role
    lookup, personal space creation, etc.) rather than hand-rolling ORM
    rows that could drift from what the service actually creates.
    """

    from quberi_api.services import onboarding

    unique = uuid.uuid4().hex[:10]
    result = await onboarding.bootstrap_organization(
        db_session,
        name=f"Test Org {unique}",
        slug=f"test-org-{unique}",
        owner_email=f"owner-{unique}@example.com",
        owner_display_name="Test Owner",
        tier="team",
    )
    return result


# ---------------------------------------------------------------------------
# Super-administrator rules
# ---------------------------------------------------------------------------
# In team and enterprise organisations the owner cannot create spaces or hold owner/admin
# on one, and nobody can give themselves access (``services.access_guard``). Most tests
# predate that and use the organisation owner as an ordinary user who creates and manages
# spaces, so the rules are off by default here. Tests of the rules ask for ``strict_rules``.

from quberi_api.services import access_guard as _access_guard  # noqa: E402

_REAL_IS_RESTRICTED_ORG = _access_guard.is_restricted_org


@pytest.fixture(autouse=True)
def _super_admin_rules_off(monkeypatch):
    async def _off(_session, *, organization_id):
        return False

    monkeypatch.setattr(_access_guard, "is_restricted_org", _off)


@pytest.fixture
def strict_rules(monkeypatch):
    """Turn the super-administrator rules back on for one test."""

    monkeypatch.setattr(_access_guard, "is_restricted_org", _REAL_IS_RESTRICTED_ORG)
