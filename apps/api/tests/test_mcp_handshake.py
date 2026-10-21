# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""The MCP JSON-RPC handshake: initialize, notifications/initialized, and discovery.

``POST /`` (mounted at the application root, see api/v1/mcp/router.py's module
docstring) speaks JSON-RPC 2.0 over Streamable HTTP. These tests pin: the
protocol version negotiation, the 202-empty-body response for a body with no
``id`` (a notification/response, never a 200 JSON body — design.md §2.1), the
405 on GET (no server push is offered), and that unauthenticated/malformed
requests behave as the design specifies.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis

from quberi_api.api import dependencies
from quberi_api.api.v1.mcp.router import mcp_router
from quberi_api.core.config import get_settings
from quberi_api.domain.identity.models import Organization
from quberi_api.services import onboarding
from quberi_api.services.mcp import protocol
from quberi_api.services.oauth import core

MCP_CLIENT = "quberi-mcp"
REDIRECT = "http://127.0.0.1:53682/callback"


def _pkce() -> tuple[str, str]:
    verifier = (
        base64.urlsafe_b64encode(uuid.uuid4().bytes + uuid.uuid4().bytes).rstrip(b"=").decode()
    )
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


@pytest.fixture
async def redis_client():
    client = Redis.from_url("redis://127.0.0.1:6379/1", decode_responses=True)
    for pattern in ("quberi:oauth:*", "quberi:external:*"):
        for key in await client.keys(pattern):
            await client.delete(key)
    yield client
    await client.aclose()


async def _org(db, *, enabled: bool = True):
    await db.commit()
    unique = uuid.uuid4().hex[:8]
    boot = await onboarding.bootstrap_organization(
        db,
        name=f"Org {unique}",
        slug=f"org-{unique}",
        owner_email=f"owner-{unique}@example.com",
        owner_display_name="Owner",
        tier="team",
    )
    org = await db.get(Organization, boot.organization_id)
    org.allow_external_tools = enabled
    await db.flush()
    await db.commit()
    return boot


async def _tokens(db, redis, boot, *, scope, user_id=None, client_id=MCP_CLIENT):
    verifier, challenge = _pkce()
    code = await core.create_authorization_code(
        redis,
        client_id=client_id,
        redirect_uri=REDIRECT,
        code_challenge=challenge,
        user_id=user_id or boot.user_id,
        organization_id=boot.organization_id,
        scope=scope,
    )
    await db.commit()
    return await core.exchange_code(
        db,
        redis,
        code=code,
        code_verifier=verifier,
        redirect_uri=REDIRECT,
        client_id=client_id,
        client_name="MCP client",
    )


@pytest.fixture
async def http(db_session, redis_client):
    app = FastAPI()
    app.state.settings = get_settings()
    app.include_router(mcp_router)
    from types import SimpleNamespace

    infra = SimpleNamespace(redis=redis_client, qdrant=object())

    async def _session():
        yield db_session

    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[dependencies.get_infrastructure] = lambda: infra
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _auth(tokens) -> dict:
    return {"Authorization": f"Bearer {tokens.access_token}"}


async def _mcp_tokens(db, redis, boot, *, scope="knowledge:read knowledge:write knowledge:upload"):
    tokens = await _tokens(db, redis, boot, scope=scope)
    await db.commit()
    return tokens


# ---------------------------------------------------------------------------
# GET: no server push is offered
# ---------------------------------------------------------------------------


async def test_get_is_405_not_a_fake_stream(http, _no_infra_unused=None):
    resp = await http.get("/")
    assert resp.status_code == 405
    # The status code is the correct answer for an MCP client, but a human
    # or a registry crawler pasting the URL into a browser should see
    # something other than an empty body (see router.py's mcp_get).
    body = resp.json()
    assert body["service"] == "Quberi MCP"
    assert "https://mcp.quberi.com" in body["notice"]
    assert body["metadata"] == "https://mcp.quberi.com/.well-known/oauth-protected-resource"


# ---------------------------------------------------------------------------
# Authentication gate: before any method dispatch
# ---------------------------------------------------------------------------


async def test_initialize_without_a_token_is_401_with_discovery_header(http):
    resp = await http.post(
        "/", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert resp.status_code == 401
    www = resp.headers["www-authenticate"]
    assert "invalid_token" in www
    assert "resource_metadata=" in www
    assert "mcp.quberi.com/.well-known/oauth-protected-resource" in www


async def test_a_harness_token_is_rejected_not_just_an_mcp_one(
    http, db_session, redis_client
):
    """design.md §2.2's minimal audience check: only client_id=quberi-mcp is accepted."""
    boot = await _org(db_session)
    harness_tokens = await _tokens(
        db_session, redis_client, boot, scope="knowledge:read", client_id="quberi-harness"
    )
    await db_session.commit()
    resp = await http.post(
        "/",
        headers=_auth(harness_tokens),
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 401
    assert "resource_metadata=" in resp.headers["www-authenticate"]


async def test_a_revoked_grant_is_401(http, db_session, redis_client):
    from sqlalchemy import select

    from quberi_api.domain.oauth.models import OAuthGrant

    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot)
    await db_session.commit()
    grant = (
        await db_session.execute(select(OAuthGrant).where(OAuthGrant.user_id == boot.user_id))
    ).scalars().first()
    grant.revoked_at = datetime.now(timezone.utc)
    grant.revoke_reason = "revoked_by_user"
    await db_session.commit()
    resp = await http.post(
        "/", headers=_auth(tokens), json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert resp.status_code == 401


async def test_the_organisation_switch_being_off_is_403(http, db_session, redis_client):
    """The switch is checked live on every call, not only at token-issue time."""
    boot = await _org(db_session, enabled=True)
    tokens = await _mcp_tokens(db_session, redis_client, boot)
    await db_session.commit()

    org = await db_session.get(Organization, boot.organization_id)
    org.allow_external_tools = False
    await db_session.commit()

    resp = await http.post(
        "/", headers=_auth(tokens), json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "external_tools_disabled"


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


async def test_initialize_negotiates_the_protocol_version(http, db_session, redis_client):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot)
    await db_session.commit()

    resp = await http.post(
        "/",
        headers=_auth(tokens),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "codex"}},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == 1
    result = body["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "quberi"


async def test_initialize_with_an_unsupported_version_offers_ours(
    http, db_session, redis_client
):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot)
    await db_session.commit()
    resp = await http.post(
        "/",
        headers=_auth(tokens),
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "initialize",
            "params": {"protocolVersion": "2099-01-01"},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["protocolVersion"] == "2025-06-18"


# ---------------------------------------------------------------------------
# notifications/initialized — no ``id``: 202, empty body, before any dispatch
# ---------------------------------------------------------------------------


async def test_a_notification_gets_202_and_an_empty_body(http, db_session, redis_client):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot)
    await db_session.commit()
    resp = await http.post(
        "/", headers=_auth(tokens), json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert resp.status_code == 202
    assert resp.content == b""


async def test_an_unknown_notification_also_gets_202_not_a_method_error(
    http, db_session, redis_client
):
    """A body with no id can never carry a JSON-RPC error response (no id to echo)."""
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot)
    await db_session.commit()
    resp = await http.post(
        "/", headers=_auth(tokens), json={"jsonrpc": "2.0", "method": "notifications/cancelled"}
    )
    assert resp.status_code == 202
    assert resp.content == b""


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------


async def test_invalid_json_is_a_parse_error(http, db_session, redis_client):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot)
    await db_session.commit()
    resp = await http.post(
        "/", headers={**_auth(tokens), "Content-Type": "application/json"}, content=b"{not json"
    )
    assert resp.status_code == 200  # JSON-RPC errors are still HTTP 200
    body = resp.json()
    assert body["error"]["code"] == protocol.PARSE_ERROR


async def test_not_jsonrpc_2_0_is_invalid_request(http, db_session, redis_client):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot)
    await db_session.commit()
    resp = await http.post("/", headers=_auth(tokens), json={"id": 1, "method": "initialize"})
    assert resp.json()["error"]["code"] == protocol.INVALID_REQUEST


async def test_an_unknown_method_is_method_not_found(http, db_session, redis_client):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot)
    await db_session.commit()
    resp = await http.post(
        "/", headers=_auth(tokens), json={"jsonrpc": "2.0", "id": 1, "method": "resources/list"}
    )
    body = resp.json()
    assert body["error"]["code"] == protocol.METHOD_NOT_FOUND
    assert body["id"] == 1
