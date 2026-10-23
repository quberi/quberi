"""GET /api/v1/admin/traffic: who may call it and what comes back.

The log is a file in ``tmp_path`` named by a settings override; every address is from a
documentation range and every host is an ``example.test`` name.
"""

from __future__ import annotations

import json
import time
import uuid

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from quberi_api.api import dependencies
from quberi_api.api.v1.admin.traffic import admin_traffic_router
from quberi_api.core.config import get_settings
from quberi_api.services import traffic_summary
from quberi_api.services.auth.session import issue_session

STATIC_TOKEN = "static-admin-token-for-tests"
VISITOR = "203.0.113.9"
EDGE = "198.51.100.7"
HOSTS = "app.example.test,api.example.test,mcp.example.test,example.test"
BROWSER = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def _line(**over) -> str:
    data = {
        "level": "info",
        "ts": time.time() - 30,
        "logger": "http.log.access.log2",
        "msg": "handled request",
        "request": {
            "remote_ip": EDGE,
            "client_ip": EDGE,
            "proto": "HTTP/2.0",
            "method": "GET",
            "host": "app.example.test",
            "uri": "/",
        },
        "status": 200,
        "user_agent": BROWSER,
        "country": "US",
        "visitor_ip": VISITOR,
    }
    data.update(over)
    return json.dumps(data)


@pytest.fixture(autouse=True)
def _fresh_cache():
    traffic_summary.cache_clear()
    yield
    traffic_summary.cache_clear()


@pytest.fixture
def log_file(tmp_path):
    path = tmp_path / "access.log"
    path.write_text(
        "\n".join(
            [
                _line(),
                _line(),
                _line(visitor_ip="192.0.2.44", user_agent="curl/8.7.1", country="CN"),
                _line(user_agent="<script>alert(1)</script>", visitor_ip="192.0.2.45"),
            ]
        )
        + "\n"
    )
    return path


def _make_client(db_session, **settings_over):
    app = FastAPI()
    app.state.settings = get_settings().model_copy(
        update={"platform_admin_token": SecretStr(STATIC_TOKEN), **settings_over}
    )

    async def _session():
        yield db_session

    app.dependency_overrides[dependencies.get_db_session] = _session
    app.include_router(admin_traffic_router, prefix="/api/v1")
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
async def client(db_session, log_file):
    async with _make_client(
        db_session, access_log_path=str(log_file), traffic_site_hosts=HOSTS
    ) as c:
        yield c


def _admin() -> dict:
    return {"Authorization": f"Bearer {STATIC_TOKEN}"}


# ---------------------------------------------------------------------------
# Who may call it
# ---------------------------------------------------------------------------


async def test_no_credentials_is_refused(client):
    resp = await client.get("/api/v1/admin/traffic")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_a_wrong_token_is_refused(client):
    resp = await client.get(
        "/api/v1/admin/traffic", headers={"Authorization": "Bearer not-the-token"}
    )
    assert resp.status_code == 401


async def test_a_member_session_is_refused(client):
    member = issue_session(
        identity_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        email="member@example.com",
    )
    resp = await client.get(
        "/api/v1/admin/traffic", headers={"Authorization": f"Bearer {member}"}
    )
    assert resp.status_code == 401


async def test_refused_callers_get_no_summary_even_when_a_log_exists(client):
    resp = await client.get("/api/v1/admin/traffic", headers={"Authorization": "Bearer x"})
    assert "totals" not in resp.text and "requests" not in resp.text


# ---------------------------------------------------------------------------
# What it returns
# ---------------------------------------------------------------------------


async def test_the_admin_token_gets_a_summary(client):
    resp = await client.get("/api/v1/admin/traffic", headers=_admin())
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True and body["reason"] is None
    assert body["totals"]["requests"] == 4
    assert body["totals"]["distinct_visitors"] == 3
    # the window key is literally "from"
    assert set(body["window"]) == {"from", "to"}
    kinds = {k["kind"]: k["requests"] for k in body["by_kind"]}
    assert kinds["browser_like"] == 2 and kinds["scanner_or_tool"] == 1 and kinds["other"] == 1
    assert body["by_site"] == [{"host": "app.example.test", "requests": 4}]
    assert body["address_note"]
    assert {"guest_questions", "sign_in_requests", "oauth_requests", "mcp_successful_posts"} == set(
        body["signals"]
    )


async def test_hours_defaults_to_24_and_is_bounded(client):
    body = (await client.get("/api/v1/admin/traffic", headers=_admin())).json()
    assert len(body["hourly"]) in (24, 25)
    for hours in (0, 337, -1):
        resp = await client.get(f"/api/v1/admin/traffic?hours={hours}", headers=_admin())
        assert resp.status_code == 422
    bad = await client.get("/api/v1/admin/traffic?hours=abc", headers=_admin())
    assert bad.status_code == 422
    for hours in (1, 336):
        assert (
            await client.get(f"/api/v1/admin/traffic?hours={hours}", headers=_admin())
        ).status_code == 200


async def test_exclude_ips_reaches_the_summary(client):
    resp = await client.get(f"/api/v1/admin/traffic?exclude_ips={VISITOR}", headers=_admin())
    body = resp.json()
    assert body["totals"]["requests"] == 2
    assert body["totals"]["distinct_visitors"] == 2


async def test_a_too_long_exclude_list_is_refused(client):
    resp = await client.get(f"/api/v1/admin/traffic?exclude_ips={'1' * 1001}", headers=_admin())
    assert resp.status_code == 422


async def test_the_response_holds_no_full_address_and_no_raw_line(client):
    resp = await client.get("/api/v1/admin/traffic", headers=_admin())
    for secret in (VISITOR, EDGE, "192.0.2.44", "192.0.2.45", "handled request", "remote_ip\":"):
        assert secret not in resp.text
    masked = {v["address"] for v in resp.json()["top_visitors"]}
    assert masked == {"203.0.113.x", "192.0.2.x"}


async def test_an_attacker_controlled_user_agent_is_plain_json_text(client):
    resp = await client.get("/api/v1/admin/traffic", headers=_admin())
    assert resp.headers["content-type"].startswith("application/json")
    agents = {v["user_agent"] for v in resp.json()["top_visitors"]}
    assert "<script>alert(1)</script>" in agents


# ---------------------------------------------------------------------------
# Disabled
# ---------------------------------------------------------------------------


async def test_not_configured_is_a_200_with_a_reason(db_session):
    async with _make_client(db_session, access_log_path="") as c:
        resp = await c.get("/api/v1/admin/traffic", headers=_admin())
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False and body["reason"] == "not_configured"
    assert body["totals"]["requests"] == 0
    assert set(body["window"]) == {"from", "to"}


async def test_a_missing_file_says_so(db_session, tmp_path):
    async with _make_client(db_session, access_log_path=str(tmp_path / "missing.log")) as c:
        resp = await c.get("/api/v1/admin/traffic", headers=_admin())
    assert resp.status_code == 200
    assert resp.json()["reason"] == "file_missing"
