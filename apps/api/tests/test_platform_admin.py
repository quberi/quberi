"""Platform admin accounts: the operator's sign-in to ``/admin``.

What matters:
* there is never a built-in password: a first-boot account gets a random one
  (and must change it) unless the operator chose one deliberately;
* passwords are stored only as salted slow hashes, and a damaged hash cannot
  crash or exhaust memory on a login;
* sign-in cannot be hammered, an unknown user looks exactly like a wrong
  password, and it fails closed when Redis is down;
* a member's session is never an admin session (and vice versa), changing the
  password ends older sessions, and an account that must still change its
  password cannot reach anything else;
* the old static token keeps working for scripts.
"""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import delete, select

from quberi_api.api import dependencies
from quberi_api.api.errors import register_exception_handlers
from quberi_api.api.v1.admin.auth import admin_auth_router
from quberi_api.api.v1.admin.stats import admin_stats_router
from quberi_api.core.config import get_settings
from quberi_api.domain.platform_admin.models import PlatformAdmin
from quberi_api.services import platform_admin as svc
from quberi_api.services.auth.session import issue_session
from quberi_api.services.security import crypto

STATIC_TOKEN = "static-admin-token-for-tests"
GOOD_PASSWORD = "correct horse battery"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def redis_client():
    client = Redis.from_url("redis://127.0.0.1:6379/1", decode_responses=True)
    for key in await client.keys("quberi:padmin:*"):
        await client.delete(key)
    yield client
    for key in await client.keys("quberi:padmin:*"):
        await client.delete(key)
    await client.aclose()


async def _clear_admins(db) -> None:
    await db.execute(delete(PlatformAdmin))
    await db.commit()


async def _make_admin(
    db, *, username="root", password=GOOD_PASSWORD, must_change=False
) -> PlatformAdmin:
    await _clear_admins(db)
    admin = PlatformAdmin(
        username=username,
        password_hash=svc.hash_password(password),
        must_change_password=must_change,
        token_version=1,
    )
    db.add(admin)
    await db.commit()
    return admin


def _settings(**over):
    base = get_settings()
    return base.model_copy(update=over)


@pytest.fixture
async def client(db_session, redis_client):
    app = FastAPI()
    app.state.settings = _settings(platform_admin_token=SecretStr(STATIC_TOKEN))
    infra = SimpleNamespace(redis=redis_client)

    async def _session():
        yield db_session

    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[dependencies.get_infrastructure] = lambda: infra
    app.include_router(admin_auth_router, prefix="/api/v1")
    app.include_router(admin_stats_router, prefix="/api/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _login(client, username="root", password=GOOD_PASSWORD):
    return await client.post(
        "/api/v1/admin/auth/login", json={"username": username, "password": password}
    )


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def test_hash_verifies_only_the_right_password():
    stored = svc.hash_password(GOOD_PASSWORD)
    assert svc.verify_password(GOOD_PASSWORD, stored)
    assert not svc.verify_password("wrong password!!", stored)
    assert not svc.verify_password("", stored)


def test_hash_is_salted_and_never_contains_the_password():
    a = svc.hash_password(GOOD_PASSWORD)
    b = svc.hash_password(GOOD_PASSWORD)
    assert a != b
    assert GOOD_PASSWORD not in a


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "plaintext",
        "scrypt$only$three$parts",
        "bcrypt$32768$8$1$c2FsdHNhbHQ$aGFzaGhhc2hoYXNoaGFzaA",
        # absurd cost parameters must be refused, not computed
        "scrypt$1073741824$8$1$c2FsdHNhbHQ$aGFzaGhhc2hoYXNoaGFzaGhhc2g",
        "scrypt$32768$1024$1$c2FsdHNhbHQ$aGFzaGhhc2hoYXNoaGFzaGhhc2g",
        "scrypt$32768$8$1000$c2FsdHNhbHQ$aGFzaGhhc2hoYXNoaGFzaGhhc2g",
        # N must be a power of two
        "scrypt$30000$8$1$c2FsdHNhbHQ$aGFzaGhhc2hoYXNoaGFzaGhhc2g",
        "scrypt$32768$8$1$!!!$???",
    ],
)
def test_a_damaged_hash_is_a_failed_login_not_a_crash(stored):
    started = time.monotonic()
    assert svc.verify_password(GOOD_PASSWORD, stored) is False
    assert time.monotonic() - started < 2.0


@pytest.mark.parametrize(
    "password,why",
    [
        ("short1", "at least"),
        ("a" * 129, "at most"),
        (" leading and trailing ", "spaces"),
        ("aaaaaaaaaaaaaaaaaaaa", "repetitive"),
        ("my-root-password-1", "username"),
    ],
)
def test_weak_new_passwords_are_refused(password, why):
    with pytest.raises(svc.PlatformAdminError) as caught:
        svc.validate_new_password(password, username="root")
    assert why in caught.value.message


# ---------------------------------------------------------------------------
# First boot
# ---------------------------------------------------------------------------


async def test_first_boot_generates_a_random_password_that_must_be_changed(
    db_session, caplog
):
    await _clear_admins(db_session)
    with caplog.at_level("WARNING"):
        generated = await svc.ensure_bootstrap_admin(
            db_session, settings=_settings(bootstrap_admin_password=None)
        )
    assert generated and len(generated) >= 16
    admin = await db_session.scalar(select(PlatformAdmin))
    assert admin.username == "admin"
    assert admin.must_change_password is True
    assert svc.verify_password(generated, admin.password_hash)
    assert generated not in admin.password_hash
    # shown to the operator exactly once, in the log
    assert sum(generated in r.getMessage() for r in caplog.records) == 1


async def test_no_built_in_default_password_exists(db_session):
    await _clear_admins(db_session)
    await svc.ensure_bootstrap_admin(db_session, settings=_settings())
    admin = await db_session.scalar(select(PlatformAdmin))
    for guess in ("admin", "password", "changeme", "admin123", "quberi", ""):
        assert not svc.verify_password(guess, admin.password_hash)


async def test_two_first_boots_do_not_share_a_password(db_session):
    await _clear_admins(db_session)
    first = await svc.ensure_bootstrap_admin(db_session, settings=_settings())
    await _clear_admins(db_session)
    second = await svc.ensure_bootstrap_admin(db_session, settings=_settings())
    assert first != second


async def test_a_password_chosen_by_the_operator_is_not_forced_to_change(db_session):
    await _clear_admins(db_session)
    generated = await svc.ensure_bootstrap_admin(
        db_session,
        settings=_settings(
            bootstrap_admin_username="Ops",
            bootstrap_admin_password=SecretStr("operator chose this one"),
        ),
    )
    assert generated is None
    admin = await db_session.scalar(select(PlatformAdmin))
    assert admin.username == "ops"
    assert admin.must_change_password is False
    assert svc.verify_password("operator chose this one", admin.password_hash)


async def test_a_weak_configured_password_is_replaced_not_accepted(db_session):
    await _clear_admins(db_session)
    generated = await svc.ensure_bootstrap_admin(
        db_session, settings=_settings(bootstrap_admin_password=SecretStr("admin"))
    )
    assert generated is not None
    admin = await db_session.scalar(select(PlatformAdmin))
    assert admin.must_change_password is True
    assert not svc.verify_password("admin", admin.password_hash)


async def test_boot_never_touches_an_existing_account(db_session):
    admin = await _make_admin(db_session)
    before = admin.password_hash
    result = await svc.ensure_bootstrap_admin(
        db_session,
        settings=_settings(
            bootstrap_admin_password=SecretStr("a different long password")
        ),
    )
    assert result is None
    rows = (await db_session.scalars(select(PlatformAdmin))).all()
    assert len(rows) == 1 and rows[0].password_hash == before


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


async def test_login_succeeds_and_returns_a_session(client, db_session):
    await _make_admin(db_session)
    resp = await _login(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "root"
    assert body["must_change_password"] is False
    assert body["expires_at"] > time.time()
    # the session works on an admin endpoint
    ok = await client.get(
        "/api/v1/admin/stats/overview", headers=_bearer(body["token"])
    )
    assert ok.status_code != 401


async def test_username_is_case_insensitive(client, db_session):
    await _make_admin(db_session, username="root")
    assert (await _login(client, username="  ROOT ")).status_code == 200


async def test_unknown_user_looks_exactly_like_a_wrong_password(client, db_session):
    await _make_admin(db_session)
    wrong = await _login(client, password="not the password")
    unknown = await _login(client, username="nobody")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


async def test_the_account_is_locked_after_repeated_failures(client, db_session):
    await _make_admin(db_session)
    for _ in range(5):
        assert (await _login(client, password="bad guess here")).status_code == 401
    # even the right password is refused now
    locked = await _login(client)
    assert locked.status_code == 429
    assert "too many" in locked.json()["detail"]


async def test_success_clears_the_failure_count(client, db_session):
    await _make_admin(db_session)
    for _ in range(4):
        await _login(client, password="bad guess here")
    assert (await _login(client)).status_code == 200
    for _ in range(4):
        assert (await _login(client, password="bad guess here")).status_code == 401
    assert (await _login(client)).status_code == 200


async def test_guessing_one_account_does_not_lock_another_address_out_of_others(
    client, db_session
):
    await _make_admin(db_session)
    for _ in range(5):
        await _login(client, password="bad guess here")
    # a different (also unknown) name is not affected by root's lock
    assert (await _login(client, username="someone-else")).status_code == 401


@pytest.mark.parametrize("broken_operation", ["incr", "get"])
async def test_signing_in_fails_closed_when_redis_is_down(
    client, db_session, redis_client, broken_operation
):
    """Each Redis operation on the sign-in path must refuse on its own.

    Only one is broken per run, so one check cannot mask the other: with ``incr``
    broken the counter can't be kept; with ``get`` broken the lockout can't be
    read. Either way a correct password must not get in unthrottled.
    """

    await _make_admin(db_session)

    class Flaky:
        def __getattr__(self, name):
            real = getattr(redis_client, name)
            if name != broken_operation:
                return real

            async def boom(*a, **k):
                raise ConnectionError("redis down")

            return boom

    client._transport.app.dependency_overrides[dependencies.get_infrastructure] = (
        lambda: SimpleNamespace(redis=Flaky())
    )
    resp = await _login(client)
    assert resp.status_code == 503
    assert "token" not in resp.json()


async def test_login_rejects_unexpected_fields(client, db_session):
    await _make_admin(db_session)
    resp = await client.post(
        "/api/v1/admin/auth/login",
        json={"username": "root", "password": GOOD_PASSWORD, "is_admin": True},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Sessions and what they can reach
# ---------------------------------------------------------------------------


async def test_a_member_session_is_not_an_admin_session(client, db_session):
    await _make_admin(db_session)
    member = issue_session(
        identity_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        email="member@example.com",
    )
    resp = await client.get("/api/v1/admin/stats/overview", headers=_bearer(member))
    assert resp.status_code == 401
    me = await client.get("/api/v1/admin/auth/me", headers=_bearer(member))
    assert me.status_code == 401


async def test_an_admin_session_is_not_a_member_session(db_session):
    from quberi_api.services.auth.session import read_session

    admin = await _make_admin(db_session)
    token, _ = svc.issue_admin_token(admin, ttl_seconds=600)
    assert read_session(token) is None


async def test_a_token_forged_with_the_wrong_type_is_refused(db_session):
    admin = await _make_admin(db_session)
    now = int(time.time())
    forged = crypto.seal(
        {"v": 1, "typ": "member", "aid": str(admin.id), "tv": 1, "exp": now + 600}
    )
    assert svc.read_admin_token(forged) is None
    assert await svc.admin_for_token(db_session, forged) is None


async def test_an_expired_session_is_refused(db_session):
    admin = await _make_admin(db_session)
    token, _ = svc.issue_admin_token(admin, ttl_seconds=-5)
    assert await svc.admin_for_token(db_session, token) is None


async def test_a_session_for_a_deleted_account_is_refused(client, db_session):
    admin = await _make_admin(db_session)
    token, _ = svc.issue_admin_token(admin, ttl_seconds=600)
    await _clear_admins(db_session)
    resp = await client.get("/api/v1/admin/auth/me", headers=_bearer(token))
    assert resp.status_code == 401


async def test_an_account_that_must_change_its_password_reaches_nothing_else(
    client, db_session
):
    await _make_admin(db_session, must_change=True)
    body = (await _login(client)).json()
    assert body["must_change_password"] is True
    headers = _bearer(body["token"])
    # can see who it is and change the password...
    assert (await client.get("/api/v1/admin/auth/me", headers=headers)).status_code == 200
    # ...but not the platform
    blocked = await client.get("/api/v1/admin/stats/overview", headers=headers)
    assert blocked.status_code == 401


async def test_changing_the_password_ends_older_sessions_and_unlocks_the_console(
    client, db_session
):
    await _make_admin(db_session, must_change=True)
    old = (await _login(client)).json()["token"]
    resp = await client.post(
        "/api/v1/admin/auth/change-password",
        headers=_bearer(old),
        json={"current_password": GOOD_PASSWORD, "new_password": "a brand new passphrase"},
    )
    assert resp.status_code == 200
    new = resp.json()
    assert new["must_change_password"] is False
    # the old session is dead, the new one works on the platform
    assert (
        await client.get("/api/v1/admin/auth/me", headers=_bearer(old))
    ).status_code == 401
    assert (
        await client.get("/api/v1/admin/stats/overview", headers=_bearer(new["token"]))
    ).status_code != 401
    # old password no longer signs in, new one does
    assert (await _login(client)).status_code == 401
    assert (await _login(client, password="a brand new passphrase")).status_code == 200


async def test_change_password_needs_the_current_password(client, db_session):
    await _make_admin(db_session)
    token = (await _login(client)).json()["token"]
    resp = await client.post(
        "/api/v1/admin/auth/change-password",
        headers=_bearer(token),
        json={"current_password": "not it at all", "new_password": "a brand new passphrase"},
    )
    assert resp.status_code == 403
    # still the old password
    assert (await _login(client)).status_code == 200


async def test_change_password_refuses_a_weak_or_unchanged_password(client, db_session):
    await _make_admin(db_session)
    token = (await _login(client)).json()["token"]
    for new in ("short", GOOD_PASSWORD):
        resp = await client.post(
            "/api/v1/admin/auth/change-password",
            headers=_bearer(token),
            json={"current_password": GOOD_PASSWORD, "new_password": new},
        )
        assert resp.status_code == 400


async def test_the_account_endpoints_do_not_accept_the_static_token(client, db_session):
    await _make_admin(db_session)
    me = await client.get("/api/v1/admin/auth/me", headers=_bearer(STATIC_TOKEN))
    assert me.status_code == 401
    change = await client.post(
        "/api/v1/admin/auth/change-password",
        headers=_bearer(STATIC_TOKEN),
        json={"current_password": "x", "new_password": "y" * 20},
    )
    assert change.status_code == 401


# ---------------------------------------------------------------------------
# The old static token still works
# ---------------------------------------------------------------------------


async def test_the_static_token_still_opens_the_admin_endpoints(client, db_session):
    await _clear_admins(db_session)
    resp = await client.get("/api/v1/admin/stats/overview", headers=_bearer(STATIC_TOKEN))
    assert resp.status_code != 401


async def test_a_wrong_or_missing_token_is_refused(client, db_session):
    await _make_admin(db_session)
    for headers in ({}, _bearer("wrong"), _bearer("")):
        resp = await client.get("/api/v1/admin/stats/overview", headers=headers)
        assert resp.status_code == 401


async def test_with_no_static_token_configured_only_sessions_work(client, db_session):
    admin = await _make_admin(db_session)
    client._transport.app.state.settings = _settings(platform_admin_token=None)
    refused = await client.get(
        "/api/v1/admin/stats/overview", headers=_bearer(STATIC_TOKEN)
    )
    assert refused.status_code == 401
    token, _ = svc.issue_admin_token(admin, ttl_seconds=600)
    ok = await client.get("/api/v1/admin/stats/overview", headers=_bearer(token))
    assert ok.status_code != 401


# ---------------------------------------------------------------------------
# Message codes (the handler is registered only here; the tests above run on an
# app without it and prove the plain ``{"detail": ...}`` fallback)
# ---------------------------------------------------------------------------


@pytest.fixture
async def coded(client):
    register_exception_handlers(client._transport.app)
    return client


async def test_without_the_handler_an_error_is_still_a_plain_detail(client, db_session):
    await _make_admin(db_session)
    resp = await _login(client, password="not the password")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "invalid username or password"}


async def test_a_bad_login_carries_a_code_and_english_text_by_default(coded, db_session):
    await _make_admin(db_session)
    resp = await _login(coded, password="not the password")
    assert resp.status_code == 401
    assert resp.json() == {
        "detail": "invalid username or password",
        "code": "admin.auth.bad_credentials",
    }


async def test_accept_language_chooses_the_text_but_not_the_code(coded, db_session):
    await _make_admin(db_session)
    for header, detail in (
        ("zh-CN", "用户名或密码不正确"),
        ("zh-CN,zh;q=0.9,en;q=0.8", "用户名或密码不正确"),
        ("en-US", "invalid username or password"),
        ("fr-FR", "invalid username or password"),
        ("???", "invalid username or password"),
    ):
        resp = await coded.post(
            "/api/v1/admin/auth/login",
            headers={"Accept-Language": header},
            json={"username": "root", "password": "not the password"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == detail
        assert resp.json()["code"] == "admin.auth.bad_credentials"


async def test_a_locked_account_answers_429_with_a_code(coded, db_session):
    await _make_admin(db_session)
    for _ in range(5):
        await _login(coded, password="bad guess here")
    locked = await _login(coded)
    assert locked.status_code == 429
    assert locked.json()["code"] == "admin.auth.too_many_attempts"
    assert locked.json()["detail"] == "too many attempts, try again later"


async def test_a_redis_outage_answers_503_with_a_code(coded, db_session, redis_client):
    await _make_admin(db_session)

    class Down:
        def __getattr__(self, name):
            async def boom(*a, **k):
                raise ConnectionError("redis down")

            return boom

    coded._transport.app.dependency_overrides[dependencies.get_infrastructure] = (
        lambda: SimpleNamespace(redis=Down())
    )
    resp = await _login(coded)
    assert resp.status_code == 503
    assert resp.json()["code"] == "admin.auth.try_again_shortly"


async def test_change_password_errors_carry_codes_and_params(coded, db_session):
    await _make_admin(db_session)
    token = (await _login(coded)).json()["token"]

    async def change(current, new, lang=None):
        headers = _bearer(token)
        if lang:
            headers["Accept-Language"] = lang
        return await coded.post(
            "/api/v1/admin/auth/change-password",
            headers=headers,
            json={"current_password": current, "new_password": new},
        )

    wrong = await change("not it at all", "a brand new passphrase")
    assert wrong.status_code == 403
    assert wrong.json()["code"] == "admin.auth.current_password_wrong"
    assert wrong.json()["detail"] == "current password is incorrect"

    short = await change(GOOD_PASSWORD, "short")
    assert short.status_code == 400
    assert short.json() == {
        "detail": "password must be at least 12 characters",
        "code": "admin.auth.password_too_short",
        "params": {"min": 12},
    }
    short_zh = await change(GOOD_PASSWORD, "short", lang="zh-CN")
    assert short_zh.json()["detail"] == "密码至少需要 12 个字符"
    assert short_zh.json()["params"] == {"min": 12}

    same = await change(GOOD_PASSWORD, GOOD_PASSWORD)
    assert same.status_code == 400
    assert same.json()["code"] == "admin.auth.password_unchanged"

    long_ = await change(GOOD_PASSWORD, "x" * 129)
    assert long_.json()["code"] == "admin.auth.password_too_long"
    assert long_.json()["params"] == {"max": 128}


async def test_a_wrong_token_and_a_missing_session_carry_codes_and_the_bearer_header(
    coded, db_session
):
    await _make_admin(db_session)
    wrong = await coded.get("/api/v1/admin/stats/overview", headers=_bearer("wrong"))
    assert wrong.status_code == 401
    assert wrong.headers["www-authenticate"] == "Bearer"
    assert wrong.json() == {
        "detail": "invalid platform admin token",
        "code": "admin.auth.invalid_token",
    }
    wrong_zh = await coded.get(
        "/api/v1/admin/stats/overview",
        headers={**_bearer("wrong"), "Accept-Language": "zh-CN"},
    )
    assert wrong_zh.json()["detail"] == "平台管理员令牌无效"

    no_session = await coded.get("/api/v1/admin/auth/me")
    assert no_session.status_code == 401
    assert no_session.headers["www-authenticate"] == "Bearer"
    assert no_session.json() == {"detail": "sign in required", "code": "admin.auth.sign_in_required"}
