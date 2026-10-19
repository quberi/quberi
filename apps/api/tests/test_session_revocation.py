"""Web sessions check the account on every request and can be revoked on the server."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import event, func, select, text, update

from quberi_api.api import dependencies
from quberi_api.api.errors import register_exception_handlers
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.api.v1.auth.router import auth_router
from quberi_api.core.config import get_settings
from quberi_api.domain.auth.models import RevokedSession
from quberi_api.domain.identity.models import Organization, User
from quberi_api.services.auth.account_status import AccountUnavailableError
from quberi_api.services.auth.session import (
    issue_session,
    mint_session,
    read_session,
    revoke_session,
    session_token_hash,
)
from quberi_api.services.security import crypto
from tests.test_image_search import _member


@pytest.fixture
async def client(db_session):
    app = FastAPI()
    app.state.settings = get_settings()
    app.include_router(auth_router, prefix="/api/v1")

    @app.get("/api/v1/ping")
    async def ping(caller: CallerContext = Depends(require_caller)):
        return {"user": str(caller.user.id)}

    register_exception_handlers(app)

    async def _session():
        yield db_session

    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[dependencies.get_infrastructure] = lambda: SimpleNamespace()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _token(db, boot, person, **kw):
    await db.commit()
    return await mint_session(
        db,
        identity_id=person.identity_id,
        user_id=person.id,
        organization_id=boot.organization_id,
        email=person.email,
        **kw,
    )


async def _revoked_count(db):
    return (await db.execute(select(func.count()).select_from(RevokedSession))).scalar_one()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


async def _set_user(db, user_id, **values):
    await db.execute(update(User).where(User.id == user_id).values(**values))
    await db.commit()


# ---------------------------------------------------------------------------
# Token format
# ---------------------------------------------------------------------------


def test_a_token_without_a_version_reads_as_version_zero():
    payload = {
        "v": 1,
        "iid": str(uuid.uuid4()),
        "uid": str(uuid.uuid4()),
        "oid": str(uuid.uuid4()),
        "email": "a@example.com",
        "g": False,
        "iat": 1,
        "exp": 4_000_000_000,
    }
    claims = read_session(crypto.seal(payload))
    assert claims is not None and claims.session_version == 0
    assert claims.expires_at == 4_000_000_000


def test_a_token_with_a_non_integer_version_is_invalid():
    payload = {
        "v": 1,
        "iid": str(uuid.uuid4()),
        "uid": str(uuid.uuid4()),
        "oid": str(uuid.uuid4()),
        "email": "a@example.com",
        "sv": "1",
        "exp": 4_000_000_000,
    }
    assert read_session(crypto.seal(payload)) is None


def test_issued_tokens_carry_the_session_version():
    token = issue_session(
        identity_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        email="a@example.com",
        session_version=3,
    )
    claims = read_session(token)
    assert claims is not None and claims.session_version == 3


# ---------------------------------------------------------------------------
# mint_session: the last gate on every sign-in path
# ---------------------------------------------------------------------------


async def test_mint_session_signs_with_the_current_version(db_session, org_and_owner):
    person = await _member(db_session, org_and_owner)
    await _set_user(db_session, person.id, session_version=4)
    token = await _token(db_session, org_and_owner, person)
    claims = read_session(token)
    assert claims is not None and claims.session_version == 4


@pytest.mark.parametrize("status", ["suspended", "archived"])
async def test_mint_session_refuses_a_blocked_user(db_session, org_and_owner, status):
    person = await _member(db_session, org_and_owner)
    await _set_user(db_session, person.id, status=status)
    with pytest.raises(AccountUnavailableError):
        await _token(db_session, org_and_owner, person)


async def test_mint_session_refuses_an_inactive_organization(db_session, org_and_owner):
    person = await _member(db_session, org_and_owner)
    await db_session.execute(
        update(Organization)
        .where(Organization.id == org_and_owner.organization_id)
        .values(status="suspended")
    )
    await db_session.commit()
    with pytest.raises(AccountUnavailableError):
        await _token(db_session, org_and_owner, person)


async def test_mint_session_still_allows_an_invited_user(db_session, org_and_owner):
    person = await _member(db_session, org_and_owner)
    await _set_user(db_session, person.id, status="invited")
    assert await _token(db_session, org_and_owner, person)


# ---------------------------------------------------------------------------
# revoke_session
# ---------------------------------------------------------------------------


async def test_revoke_session_is_idempotent_and_stores_only_a_hash(db_session, org_and_owner):
    person = await _member(db_session, org_and_owner)
    token = await _token(db_session, org_and_owner, person)
    await revoke_session(db_session, token)
    await revoke_session(db_session, token)
    await db_session.commit()
    rows = (await db_session.execute(select(RevokedSession))).scalars().all()
    assert [r.token_hash for r in rows] == [session_token_hash(token)]
    assert token not in rows[0].token_hash


async def test_revoke_session_ignores_junk_and_guest_tokens(db_session, org_and_owner):
    await revoke_session(db_session, "not-a-token")
    guest = issue_session(
        identity_id=uuid.uuid4(),
        user_id=org_and_owner.user_id,
        organization_id=org_and_owner.organization_id,
        email="g@example.com",
        is_guest=True,
    )
    await revoke_session(db_session, guest)
    await db_session.commit()
    assert await _revoked_count(db_session) == 0


async def test_revoking_for_a_missing_user_inserts_nothing_and_keeps_the_transaction_usable(
    db_session, org_and_owner
):
    token = issue_session(
        identity_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        organization_id=org_and_owner.organization_id,
        email="gone@example.com",
    )
    await revoke_session(db_session, token)
    assert (await db_session.execute(text("select 1"))).scalar_one() == 1
    assert await _revoked_count(db_session) == 0


async def test_a_revoked_session_row_goes_with_its_user(db_session, org_and_owner):
    person = await _member(db_session, org_and_owner)
    token = await _token(db_session, org_and_owner, person)
    await revoke_session(db_session, token)
    await db_session.commit()
    await db_session.execute(text("delete from users where id = :i"), {"i": person.id})
    assert await _revoked_count(db_session) == 0


# ---------------------------------------------------------------------------
# require_caller over HTTP
# ---------------------------------------------------------------------------


async def test_a_normal_session_works(client, db_session, org_and_owner):
    person = await _member(db_session, org_and_owner)
    token = await _token(db_session, org_and_owner, person)
    resp = await client.get("/api/v1/ping", headers=_auth(token))
    assert resp.status_code == 200, resp.text


async def test_an_old_token_without_a_version_still_works_for_an_active_user(
    client, db_session, org_and_owner
):
    person = await _member(db_session, org_and_owner)
    legacy = crypto.seal(
        {
            "v": 1,
            "iid": str(person.identity_id),
            "uid": str(person.id),
            "oid": str(org_and_owner.organization_id),
            "email": person.email,
            "g": False,
            "iat": 1,
            "exp": 4_000_000_000,
        }
    )
    resp = await client.get("/api/v1/ping", headers=_auth(legacy))
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("status", ["suspended", "archived"])
async def test_a_blocked_user_gets_session_invalid(client, db_session, org_and_owner, status):
    person = await _member(db_session, org_and_owner)
    token = await _token(db_session, org_and_owner, person)
    await _set_user(db_session, person.id, status=status)
    resp = await client.get("/api/v1/ping", headers=_auth(token))
    assert resp.status_code == 401
    assert resp.json()["code"] == "session_invalid"
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_bumping_the_version_signs_out_every_old_token(client, db_session, org_and_owner):
    person = await _member(db_session, org_and_owner)
    token = await _token(db_session, org_and_owner, person)
    legacy = crypto.seal(
        {
            "v": 1,
            "iid": str(person.identity_id),
            "uid": str(person.id),
            "oid": str(org_and_owner.organization_id),
            "email": person.email,
            "exp": 4_000_000_000,
        }
    )
    await _set_user(db_session, person.id, session_version=1)
    for old in (token, legacy):
        resp = await client.get("/api/v1/ping", headers=_auth(old))
        assert resp.status_code == 401 and resp.json()["code"] == "session_invalid"
    fresh = await _token(db_session, org_and_owner, person)
    assert (await client.get("/api/v1/ping", headers=_auth(fresh))).status_code == 200


async def test_a_session_of_an_inactive_organization_is_invalid(client, db_session, org_and_owner):
    person = await _member(db_session, org_and_owner)
    token = await _token(db_session, org_and_owner, person)
    await db_session.execute(
        update(Organization)
        .where(Organization.id == org_and_owner.organization_id)
        .values(status="archived")
    )
    await db_session.commit()
    resp = await client.get("/api/v1/ping", headers=_auth(token))
    assert resp.status_code == 401 and resp.json()["code"] == "session_invalid"


async def test_a_session_for_a_missing_user_is_invalid(client, db_session, org_and_owner):
    token = issue_session(
        identity_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        organization_id=org_and_owner.organization_id,
        email="gone@example.com",
    )
    resp = await client.get("/api/v1/ping", headers=_auth(token))
    assert resp.status_code == 401 and resp.json()["code"] == "session_invalid"


async def test_logout_ends_only_that_session(client, db_session, org_and_owner):
    person = await _member(db_session, org_and_owner)
    first = await _token(db_session, org_and_owner, person)
    second = await _token(db_session, org_and_owner, person)
    assert first != second
    resp = await client.post("/api/v1/auth/logout", headers=_auth(first))
    assert resp.status_code == 204
    gone = await client.get("/api/v1/ping", headers=_auth(first))
    assert gone.status_code == 401 and gone.json()["code"] == "session_invalid"
    assert (await client.get("/api/v1/ping", headers=_auth(second))).status_code == 200


async def test_logout_twice_and_without_a_token_are_both_204(client, db_session, org_and_owner):
    person = await _member(db_session, org_and_owner)
    token = await _token(db_session, org_and_owner, person)
    assert (await client.post("/api/v1/auth/logout", headers=_auth(token))).status_code == 204
    assert (await client.post("/api/v1/auth/logout", headers=_auth(token))).status_code == 204
    assert (await client.post("/api/v1/auth/logout")).status_code == 204
    assert (await client.post("/api/v1/auth/logout", headers=_auth("junk"))).status_code == 204


async def test_a_suspended_member_can_still_call_logout(client, db_session, org_and_owner):
    person = await _member(db_session, org_and_owner)
    token = await _token(db_session, org_and_owner, person)
    await _set_user(db_session, person.id, status="suspended")
    assert (await client.post("/api/v1/auth/logout", headers=_auth(token))).status_code == 204


async def test_logout_for_a_missing_user_is_204_and_the_session_stays_usable(
    client, db_session, org_and_owner
):
    token = issue_session(
        identity_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        organization_id=org_and_owner.organization_id,
        email="gone@example.com",
    )
    assert (await client.post("/api/v1/auth/logout", headers=_auth(token))).status_code == 204
    assert (await db_session.execute(text("select 1"))).scalar_one() == 1


async def test_a_wrong_verification_code_is_a_plain_401_not_session_invalid(client):
    resp = await client.post(
        "/api/v1/auth/verify", json={"email": "nobody@example.com", "code": "000000"}
    )
    assert resp.status_code == 401
    assert resp.json().get("code") != "session_invalid"


async def test_require_caller_costs_one_select(client, db_session, org_and_owner):
    person = await _member(db_session, org_and_owner)
    token = await _token(db_session, org_and_owner, person)
    seen: list[str] = []

    def _record(conn, cursor, statement, *_):
        seen.append(statement)

    bind = db_session.sync_session.get_bind()
    event.listen(bind, "before_cursor_execute", _record)
    try:
        resp = await client.get("/api/v1/ping", headers=_auth(token))
    finally:
        event.remove(bind, "before_cursor_execute", _record)
    assert resp.status_code == 200
    reads = [s for s in seen if s.lstrip().upper().startswith("SELECT")]
    assert len(reads) == 1, reads
    assert "revoked_sessions" in reads[0]
# ---------------------------------------------------------------------------
# Platform-token impersonation (development only) follows the same rules
# ---------------------------------------------------------------------------
@pytest.fixture
async def dev_client(db_session):
    from pydantic import SecretStr
    app = FastAPI()
    app.state.settings = get_settings().model_copy(
        update={"environment": "development", "platform_admin_token": SecretStr("dev-token-for-tests")}
    )
    @app.get("/api/v1/ping")
    async def ping(caller: CallerContext = Depends(require_caller)):
        return {"user": str(caller.user.id)}
    register_exception_handlers(app)
    async def _session():
        yield db_session
    app.dependency_overrides[dependencies.get_db_session] = _session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
async def _impersonate(client, db, boot, person):
    slug = (
        await db.execute(select(Organization.slug).where(Organization.id == boot.organization_id))
    ).scalar_one()
    await db.commit()
    return await client.get(
        "/api/v1/ping",
        headers={
            "Authorization": "Bearer dev-token-for-tests",
            "X-Quberi-Organization": slug,
            "X-Quberi-User-Email": person.email,
        },
    )
async def test_impersonation_with_the_platform_token_is_refused_for_a_suspended_user(
    dev_client, db_session, org_and_owner
):
    person = await _member(db_session, org_and_owner)
    assert (await _impersonate(dev_client, db_session, org_and_owner, person)).status_code == 200
    await _set_user(db_session, person.id, status="suspended")
    assert (await _impersonate(dev_client, db_session, org_and_owner, person)).status_code == 401
async def test_impersonation_with_the_platform_token_is_refused_in_an_inactive_organization(
    dev_client, db_session, org_and_owner
):
    person = await _member(db_session, org_and_owner)
    await db_session.execute(
        update(Organization)
        .where(Organization.id == org_and_owner.organization_id)
        .values(status="suspended")
    )
    await db_session.commit()
    assert (await _impersonate(dev_client, db_session, org_and_owner, person)).status_code == 401
# ---------------------------------------------------------------------------
# Clean-up of signed-out records
# ---------------------------------------------------------------------------
async def test_the_clean_up_removes_only_records_whose_token_has_expired(db_session, org_and_owner):
    from datetime import datetime, timedelta, timezone
    from quberi_api.workers.rag import purge_revoked_sessions
    person = await _member(db_session, org_and_owner)
    now = datetime.now(timezone.utc)
    db_session.add_all(
        [
            RevokedSession(token_hash="a" * 64, user_id=person.id, expires_at=now - timedelta(hours=1)),
            RevokedSession(token_hash="b" * 64, user_id=person.id, expires_at=now + timedelta(days=1)),
        ]
    )
    await db_session.commit()
    class _Factory:
        def __call__(self):
            return self
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *exc):
            return False
    removed = await purge_revoked_sessions({"session_factory": _Factory()})
    assert removed == 1
    left = (await db_session.execute(select(RevokedSession.token_hash))).scalars().all()
    assert left == ["b" * 64]
