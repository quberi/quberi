# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""Which tools may ask for access is configuration a platform admin edits.

What matters: the built-in tools keep working exactly as before; a tool that names itself by URL
(ChatGPT, Claude Code) is matched strictly and can only be sent its code where the admin said;
nothing is ever fetched from the URL; the server metadata only claims metadata-document support
while such a tool is switched on; and the admin cannot save a rule that would send codes to an IP
address, over plain http, or to a wildcard.
"""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from quberi_api.api import dependencies
from quberi_api.api.security import require_platform_admin
from quberi_api.api.v1.admin.oauth_clients import admin_oauth_clients_router
from quberi_api.api.v1.oauth import oauth_router, well_known_router
from quberi_api.core.config import get_settings
from quberi_api.domain.oauth.models import OAuthClientConfig, OAuthGrant
from quberi_api.services.oauth import clients
from tests.test_oauth_external import _login, _org, _pkce, http, redis_client  # noqa: F401

CHATGPT = "https://chatgpt.com/oauth/abc123/client.json"
CHATGPT_CALLBACK = "https://chatgpt.com/connector/oauth/abc123"
CLAUDE_CODE = "https://claude.ai/oauth/claude-code-client-metadata"
LOOPBACK = "http://127.0.0.1:53682/callback"


@pytest.fixture
async def admin(db_session, redis_client):
    """The platform admin API, with the admin check lifted (it has its own tests)."""
    app = FastAPI()
    app.state.settings = get_settings()
    app.include_router(admin_oauth_clients_router, prefix="/api/v1")
    app.include_router(oauth_router, prefix="/api/v1")
    app.include_router(well_known_router)
    infra = SimpleNamespace(redis=redis_client, qdrant=object())

    async def _session():
        yield db_session

    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[dependencies.get_infrastructure] = lambda: infra
    app.dependency_overrides[require_platform_admin] = lambda: None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _resolve(db, client_id):
    await db.commit()
    found = await clients.resolve_client(db, client_id)
    await db.commit()
    return found


# --------------------------------------------------------------------------
# The built-in tools carry over
# --------------------------------------------------------------------------


async def test_the_tools_the_code_used_to_hold_are_still_there(db_session):
    harness = await _resolve(db_session, "quberi-harness")
    mcp = await _resolve(db_session, "quberi-mcp")

    assert harness.name == "DeepSeek Harness" and harness.redirect_allowed(LOOPBACK)
    # The REST-only tool is still kept off the MCP server, as before.
    assert harness.allow_mcp is False and mcp.allow_mcp is True
    assert not harness.redirect_allowed("https://claude.ai/api/mcp/auth_callback")
    assert mcp.name == "MCP client"
    assert mcp.redirect_allowed(LOOPBACK) and mcp.redirect_allowed("https://claude.ai/api/mcp/auth_callback")


async def test_an_unknown_or_oversized_client_id_is_refused(db_session):
    assert await _resolve(db_session, "nope") is None
    assert await _resolve(db_session, "") is None
    assert await _resolve(db_session, "https://chatgpt.com/oauth/" + "a" * 300 + "/client.json") is None


# --------------------------------------------------------------------------
# Tools that name themselves by URL
# --------------------------------------------------------------------------


async def test_chatgpt_is_matched_by_pattern_and_may_only_use_its_own_callback(db_session):
    found = await _resolve(db_session, CHATGPT)

    assert found.name == "ChatGPT" and found.captured_id == "abc123"
    assert found.redirect_allowed(CHATGPT_CALLBACK)
    # The callback must carry the same id as the client id.
    assert not found.redirect_allowed("https://chatgpt.com/connector/oauth/zzz999")
    for wrong in (
        LOOPBACK,
        "http://chatgpt.com/connector/oauth/abc123",
        "https://chatgpt.com/connector/oauth/abc123/",
        "https://chatgpt.com/connector/oauth/abc123?x=1",
        "https://chatgpt.com.evil.com/connector/oauth/abc123",
        "https://evil.com/connector/oauth/abc123",
        "https://chatgpt.com/connector_platform_oauth_redirect",
    ):
        assert not found.redirect_allowed(wrong), wrong


@pytest.mark.parametrize(
    "client_id",
    [
        "https://chatgpt.com/oauth//client.json",
        "https://chatgpt.com/oauth/a/b/client.json",
        "https://chatgpt.com/oauth/abc123/client.json/extra",
        "https://chatgpt.com/oauth/abc123/client.json?x=1",
        "https://chatgpt.com.evil.com/oauth/abc123/client.json",
        "http://chatgpt.com/oauth/abc123/client.json",
        "https://evil.com/oauth/abc123/client.json",
        "https://chatgpt.com/oauth/" + "a" * 65 + "/client.json",
        "https://chatgpt.com/oauth/ab c/client.json",
    ],
)
async def test_lookalike_urls_do_not_match_the_chatgpt_pattern(db_session, client_id):
    assert await _resolve(db_session, client_id) is None


async def test_claude_code_is_matched_exactly_and_uses_loopback(db_session):
    found = await _resolve(db_session, CLAUDE_CODE)

    assert found.name == "Claude Code"
    assert found.redirect_allowed(LOOPBACK) and found.redirect_allowed("http://localhost:3118/callback")
    assert not found.redirect_allowed("https://claude.ai/api/mcp/auth_callback")
    assert await _resolve(db_session, CLAUDE_CODE + "x") is None


async def test_a_tool_switched_off_cannot_be_resolved(db_session):
    await db_session.execute(
        update(OAuthClientConfig).where(OAuthClientConfig.client_id == "quberi-mcp").values(enabled=False)
    )
    assert await _resolve(db_session, "quberi-mcp") is None


# --------------------------------------------------------------------------
# What an admin may save
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_id, kind, redirects",
    [
        ("my-tool", "registered", ["loopback"]),
        ("https://app.example.com/client.json", "metadata_url", ["https://app.example.com/cb"]),
        ("https://app.example.com/{id}/client.json", "metadata_url", ["https://app.example.com/cb/{id}", "loopback"]),
    ],
)
def test_sensible_configuration_is_accepted(client_id, kind, redirects):
    cid, rules = clients.validate_client_config(client_id, kind, redirects)
    assert cid == client_id and rules == redirects


@pytest.mark.parametrize(
    "client_id, kind, redirects",
    [
        ("my tool", "registered", ["loopback"]),  # spaces in a registered id
        ("x" * 65, "registered", ["loopback"]),
        ("http://app.example.com/client.json", "metadata_url", ["loopback"]),  # not https
        ("https://10.0.0.1/client.json", "metadata_url", ["loopback"]),  # an IP address
        ("https://localhost/client.json", "metadata_url", ["loopback"]),
        ("https://app.example.com:8443/client.json", "metadata_url", ["loopback"]),  # a port
        ("https://app.example.com/client.json?x=1", "metadata_url", ["loopback"]),
        ("https://user@app.example.com/client.json", "metadata_url", ["loopback"]),
        ("https://*.example.com/client.json", "metadata_url", ["loopback"]),  # wildcard
        ("https://{id}.example.com/client.json", "metadata_url", ["loopback"]),  # {id} in the host
        ("https://app.example.com/{id}/{id}.json", "metadata_url", ["loopback"]),  # twice
        ("my-tool", "registered", ["http://evil.com/cb"]),  # plain http
        ("my-tool", "registered", ["https://10.0.0.1/cb"]),
        ("my-tool", "registered", ["https://evil.com/*"]),
        ("my-tool", "registered", ["https://evil.com/cb?x=1"]),
        ("my-tool", "registered", ["https://evil.com:444/cb"]),
        ("my-tool", "registered", ["https://evil.com/cb/{id}"]),  # {id} but the client id has none
        ("my-tool", "registered", ["http://127.0.0.1/callback"]),  # loopback is a word, not an address
        ("my-tool", "registered", []),
        ("my-tool", "nonsense", ["loopback"]),
    ],
)
def test_dangerous_or_malformed_configuration_is_refused(client_id, kind, redirects):
    with pytest.raises(clients.ClientConfigError):
        clients.validate_client_config(client_id, kind, redirects)


# --------------------------------------------------------------------------
# Through the admin API
# --------------------------------------------------------------------------


async def test_the_admin_lists_the_tools_and_sees_whether_metadata_documents_are_advertised(admin, db_session):
    await db_session.commit()
    resp = await admin.get("/api/v1/admin/oauth-clients")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {i["client_id"] for i in body["items"]} >= {"quberi-harness", "quberi-mcp", CLAUDE_CODE}
    assert body["advertises_metadata_documents"] is True


async def test_an_admin_can_add_edit_switch_off_and_remove_a_tool(admin, db_session):
    await db_session.commit()
    created = await admin.post(
        "/api/v1/admin/oauth-clients",
        json={
            "client_id": "https://claude.example.com/{id}/meta.json",
            "kind": "metadata_url",
            "name": "Example",
            "redirect_uris": ["https://claude.example.com/cb/{id}"],
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["allow_mcp"] is True  # a new tool is an MCP tool unless said otherwise
    row_id = created.json()["id"]
    assert (await _resolve(db_session, "https://claude.example.com/q1/meta.json")).name == "Example"

    renamed = await admin.patch(f"/api/v1/admin/oauth-clients/{row_id}", json={"name": "Example App"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Example App"

    off = await admin.patch(f"/api/v1/admin/oauth-clients/{row_id}", json={"enabled": False})
    assert off.json()["enabled"] is False
    assert await _resolve(db_session, "https://claude.example.com/q1/meta.json") is None

    gone = await admin.delete(f"/api/v1/admin/oauth-clients/{row_id}")
    assert gone.status_code == 204
    assert (await admin.delete(f"/api/v1/admin/oauth-clients/{row_id}")).status_code == 404


async def test_the_admin_api_refuses_a_dangerous_rule_and_a_duplicate(admin, db_session):
    await db_session.commit()
    bad = await admin.post(
        "/api/v1/admin/oauth-clients",
        json={"client_id": "evil", "kind": "registered", "name": "Evil", "redirect_uris": ["http://evil.com/cb"]},
    )
    assert bad.status_code == 400 and "https" in bad.json()["detail"]

    dup = await admin.post(
        "/api/v1/admin/oauth-clients",
        json={"client_id": "quberi-mcp", "kind": "registered", "name": "Again", "redirect_uris": ["loopback"]},
    )
    assert dup.status_code == 409

    # A later edit is checked just as strictly as a new tool.
    mcp = (await db_session.execute(select(OAuthClientConfig).where(OAuthClientConfig.client_id == "quberi-mcp"))).scalar_one()
    await db_session.commit()
    edit = await admin.patch(f"/api/v1/admin/oauth-clients/{mcp.id}", json={"redirect_uris": ["https://10.0.0.1/x"]})
    assert edit.status_code == 400


async def test_the_admin_api_needs_a_platform_admin(db_session, redis_client):
    app = FastAPI()
    app.state.settings = get_settings()
    app.include_router(admin_oauth_clients_router, prefix="/api/v1")

    async def _session():
        yield db_session

    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[dependencies.get_infrastructure] = lambda: SimpleNamespace(redis=redis_client)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/api/v1/admin/oauth-clients")).status_code == 401
        assert (await c.post("/api/v1/admin/oauth-clients", json={})).status_code in (401, 422)


# --------------------------------------------------------------------------
# The server metadata follows the configuration
# --------------------------------------------------------------------------


async def test_metadata_claims_support_only_while_a_url_tool_is_switched_on(admin, db_session):
    await db_session.commit()
    on = (await admin.get("/.well-known/oauth-authorization-server")).json()
    assert on["client_id_metadata_document_supported"] is True
    assert on["token_endpoint_auth_methods_supported"] == ["none"]  # Claude needs this too

    await db_session.execute(
        update(OAuthClientConfig).where(OAuthClientConfig.kind == "metadata_url").values(enabled=False)
    )
    await db_session.commit()
    off = (await admin.get("/.well-known/oauth-authorization-server")).json()
    assert "client_id_metadata_document_supported" not in off


# --------------------------------------------------------------------------
# A whole ChatGPT-style login, and what we learn from a refused one
# --------------------------------------------------------------------------


async def test_a_tool_that_names_itself_by_url_can_log_in_and_the_grant_keeps_the_url(
    http, db_session, redis_client
):
    boot = await _org(db_session, enabled=True)
    headers = await _login(db_session, boot)

    consent = await http.get(
        "/api/v1/oauth/consent",
        headers=headers,
        params={"client_id": CHATGPT, "redirect_uri": CHATGPT_CALLBACK},
    )
    assert consent.status_code == 200, consent.text
    assert consent.json()["client_name"] == "ChatGPT"  # our name for it, not one the tool chose

    verifier, challenge = _pkce()
    auth = await http.post(
        "/api/v1/oauth/authorize",
        headers=headers,
        json={
            "client_id": CHATGPT,
            "redirect_uri": CHATGPT_CALLBACK,
            "state": "s",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "organization_id": str(boot.organization_id),
        },
    )
    assert auth.status_code == 200, auth.text
    target = urlsplit(auth.json()["redirect_to"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == CHATGPT_CALLBACK
    code = parse_qs(target.query)["code"][0]

    tok = await http.post(
        "/api/v1/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": CHATGPT_CALLBACK,
            "client_id": CHATGPT,
        },
    )
    assert tok.status_code == 200, tok.text

    await db_session.commit()
    grant = (await db_session.execute(select(OAuthGrant).where(OAuthGrant.user_id == boot.user_id))).scalar_one()
    assert grant.client_id == CHATGPT and grant.client_name == "ChatGPT"

    # And it can refresh with the same URL; a different URL cannot.
    refreshed = await http.post(
        "/api/v1/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": tok.json()["refresh_token"], "client_id": CHATGPT},
    )
    assert refreshed.status_code == 200, refreshed.text


async def test_a_callback_for_another_connection_is_refused_and_remembered(http, db_session, redis_client):
    boot = await _org(db_session, enabled=True)
    headers = await _login(db_session, boot)

    resp = await http.get(
        "/api/v1/oauth/consent",
        headers=headers,
        params={"client_id": CHATGPT, "redirect_uri": "https://chatgpt.com/connector/oauth/other"},
    )
    assert resp.status_code == 400 and "redirect" in resp.json()["detail"]

    unknown = await http.get(
        "/api/v1/oauth/consent",
        headers=headers,
        params={"client_id": "https://claude.ai/oauth/hosted-claude.json", "redirect_uri": "https://claude.ai/api/mcp/auth_callback"},
    )
    assert unknown.status_code == 400 and unknown.json()["detail"] == "unknown application"

    seen = await clients.recent_rejected(redis_client)
    by_client = {r["client_id"]: r for r in seen}
    assert by_client[CHATGPT]["reason"] == "redirect_not_allowed"
    hosted = by_client["https://claude.ai/oauth/hosted-claude.json"]
    assert hosted["reason"] == "unknown_client"
    assert hosted["redirect_uri"] == "https://claude.ai/api/mcp/auth_callback"


async def test_the_token_endpoint_does_not_record_refusals_it_cannot_attribute(http, redis_client):
    await http.post(
        "/api/v1/oauth/token",
        data={"grant_type": "authorization_code", "client_id": "https://evil.example/c.json"},
    )
    assert await clients.recent_rejected(redis_client) == []


async def test_the_list_of_refused_attempts_is_capped(redis_client):
    for n in range(clients.REJECTED_KEEP + 15):
        await clients.record_rejected(redis_client, client_id=f"c{n}", redirect_uri="x", reason="unknown_client")
    assert len(await clients.recent_rejected(redis_client)) == clients.REJECTED_KEEP


async def test_the_admin_list_forgets_refused_attempts_that_would_be_allowed_now(admin, db_session, redis_client):
    await db_session.commit()
    codex = "https://chatgpt.com/oauth/codex/A1VfCm6C2hld/client.json"
    await clients.record_rejected(
        redis_client, client_id=codex, redirect_uri="http://127.0.0.1:54792/callback/A1VfCm6C2hld",
        reason="unknown_client",
    )
    await clients.record_rejected(
        redis_client, client_id="quberi-mcp", redirect_uri="https://elsewhere.example/cb",
        reason="redirect_not_allowed",
    )
    await clients.record_rejected(
        redis_client, client_id=CHATGPT, redirect_uri=CHATGPT_CALLBACK, reason="unknown_client",
    )

    before = (await admin.get("/api/v1/admin/oauth-clients")).json()["rejected"]
    seen = {(r["client_id"], r["redirect_uri"]) for r in before}
    # The Codex one is not allowed yet; a wrong callback for a known tool is still wrong;
    # the ChatGPT one is allowed already (it was only refused before it was configured).
    assert (codex, "http://127.0.0.1:54792/callback/A1VfCm6C2hld") in seen
    assert ("quberi-mcp", "https://elsewhere.example/cb") in seen
    assert (CHATGPT, CHATGPT_CALLBACK) not in seen

    created = await admin.post(
        "/api/v1/admin/oauth-clients",
        json={
            "client_id": "https://chatgpt.com/oauth/codex/{id}/client.json",
            "kind": "metadata_url",
            "name": "Codex (ChatGPT app)",
            "redirect_uris": ["loopback"],
        },
    )
    assert created.status_code == 201, created.text

    after = (await admin.get("/api/v1/admin/oauth-clients")).json()["rejected"]
    assert {(r["client_id"], r["redirect_uri"]) for r in after} == {("quberi-mcp", "https://elsewhere.example/cb")}


async def test_cursor_is_a_built_in_tool_that_may_only_use_its_own_machine(db_session):
    cursor = await _resolve(db_session, "quberi-cursor")

    assert cursor.name == "Cursor" and cursor.allow_mcp is True
    assert cursor.redirect_allowed("http://localhost:8787/callback")  # what Cursor's desktop app uses
    assert not cursor.redirect_allowed("https://www.cursor.com/agents/mcp/oauth/callback")
    assert not cursor.redirect_allowed("cursor://anysphere.cursor-mcp/oauth/callback")
    assert not cursor.redirect_allowed("https://claude.ai/api/mcp/auth_callback")
