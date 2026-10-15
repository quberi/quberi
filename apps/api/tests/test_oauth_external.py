"""OAuth for outside tools: the login flow, the tokens, and the switch.

An outside tool (DeepSeek Harness) signs in with an authorization code + PKCE
and then searches the person's knowledge with an access token. What matters:

* the code works once, only with the right verifier / redirect / client;
* tokens are stored hashed, rotate on refresh, and a refresh token used twice
  kills the grant (it was copied);
* the organisation switch is checked on *every* call, so turning it off cuts
  access at once and it is off by default;
* a grant only ever acts as the person who gave it, in the organisation they
  chose - nothing the tool sends can change that;
* the token endpoint cannot be hammered, and fails closed if Redis is down.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import select, text

from quberi_api.api import dependencies
from quberi_api.api.v1 import external as external_api
from quberi_api.api.v1.external import external_router
from quberi_api.api.v1.me_external_access import me_external_access_router
from quberi_api.api.v1.oauth import oauth_router, well_known_router
from quberi_api.core.config import get_settings
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.identity.models import Organization, User
from quberi_api.domain.oauth.models import OAuthGrant
from quberi_api.services import onboarding
from quberi_api.services.auth.session import issue_session
from quberi_api.services.oauth import clients, core

REDIRECT = "http://127.0.0.1:53682/callback"
CLIENT = "quberi-harness"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(uuid.uuid4().bytes + uuid.uuid4().bytes).rstrip(
        b"="
    ).decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


@pytest.fixture
async def redis_client():
    client = Redis.from_url("redis://127.0.0.1:6379/1", decode_responses=True)
    for key in await client.keys("quberi:oauth:*"):
        await client.delete(key)
    for key in await client.keys("quberi:external:*"):
        await client.delete(key)
    yield client
    await client.aclose()


async def _org(db, *, enabled: bool):
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


async def _tokens(db, redis, boot, *, scope="knowledge:read"):
    """Run the code + PKCE exchange for the org's owner."""

    verifier, challenge = _pkce()
    code = await core.create_authorization_code(
        redis,
        client_id=CLIENT,
        redirect_uri=REDIRECT,
        code_challenge=challenge,
        user_id=boot.user_id,
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
        client_id=CLIENT,
        client_name="DeepSeek Harness",
    )


async def _grant(db, grant_id) -> OAuthGrant:
    await db.commit()
    return (
        await db.execute(select(OAuthGrant).where(OAuthGrant.id == grant_id))
    ).scalar_one()


def _error_code(exc_info) -> str:
    return exc_info.value.error


# ---------------------------------------------------------------------------
# Redirect rule and PKCE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "uri,ok",
    [
        ("http://127.0.0.1:53682/callback", True),
        ("http://[::1]:53682/callback", True),
        ("https://127.0.0.1:53682/callback", False),
        ("http://localhost:53682/callback", False),
        ("http://127.0.0.1.evil.com:53682/callback", False),
        ("http://evil.com:53682/callback", False),
        ("http://127.0.0.1/callback", False),
        ("http://127.0.0.1:80/callback", False),
        ("http://127.0.0.1:53682/other", False),
        ("http://127.0.0.1:53682/callback?x=1", False),
        ("http://127.0.0.1:53682/callback#frag", False),
        ("http://user@127.0.0.1:53682/callback", False),
        ("http://127.0.0.1:99999/callback", False),
        ("", False),
        ("http://127.0.0.1:53682/callback" + "a" * 300, False),
    ],
)
def test_redirect_must_be_a_loopback_callback(uri, ok):
    assert clients.is_valid_redirect_uri(uri) is ok


def test_pkce_only_accepts_the_matching_s256_verifier():
    verifier, challenge = _pkce()
    assert core.verify_pkce(verifier, challenge)
    assert not core.verify_pkce(verifier + "x", challenge)
    assert not core.verify_pkce("short", challenge)
    # The plain method is not supported: the challenge itself is not a verifier.
    assert not core.verify_pkce(challenge, challenge)
    assert not core.verify_pkce("", challenge)


def test_unknown_scopes_are_refused():
    # Naming nothing stays read-only (write must be asked for explicitly), so
    # widening SUPPORTED_SCOPES never retroactively widens an existing grant.
    assert clients.normalise_scope(None) == "knowledge:read"
    assert clients.normalise_scope("knowledge:read") == "knowledge:read"
    # write is now a supported scope, and read+write may be requested together.
    assert clients.normalise_scope("knowledge:write") == "knowledge:write"
    assert (
        clients.normalise_scope("knowledge:read knowledge:write")
        == "knowledge:read knowledge:write"
    )
    # ...but an unknown scope still poisons the whole request.
    assert clients.normalise_scope("admin") is None
    assert clients.normalise_scope("knowledge:read admin") is None


# ---------------------------------------------------------------------------
# Codes
# ---------------------------------------------------------------------------


async def test_a_code_works_once_and_tokens_are_stored_hashed(db_session, redis_client):
    boot = await _org(db_session, enabled=True)
    issued = await _tokens(db_session, redis_client, boot)

    assert issued.access_token.startswith("qbr_at_")
    assert issued.refresh_token.startswith("qbr_rt_")
    grant = await _grant(db_session, issued.grant_id)
    assert grant.access_hash == core.hash_token(issued.access_token)
    # Nothing in the table is the token itself.
    row = (
        await db_session.execute(
            text("select row_to_json(g)::text from oauth_grants g where id = :i"),
            {"i": issued.grant_id},
        )
    ).scalar_one()
    assert issued.access_token not in row
    assert issued.refresh_token not in row


async def test_the_same_code_cannot_be_used_twice(db_session, redis_client):
    boot = await _org(db_session, enabled=True)
    verifier, challenge = _pkce()
    code = await core.create_authorization_code(
        redis_client,
        client_id=CLIENT,
        redirect_uri=REDIRECT,
        code_challenge=challenge,
        user_id=boot.user_id,
        organization_id=boot.organization_id,
        scope="knowledge:read",
    )
    await db_session.commit()
    args = dict(
        code=code,
        code_verifier=verifier,
        redirect_uri=REDIRECT,
        client_id=CLIENT,
        client_name="x",
    )
    await core.exchange_code(db_session, redis_client, **args)
    await db_session.commit()
    with pytest.raises(core.OAuthError) as exc:
        await core.exchange_code(db_session, redis_client, **args)
    assert _error_code(exc) == "invalid_grant"


async def test_a_wrong_verifier_burns_the_code(db_session, redis_client):
    boot = await _org(db_session, enabled=True)
    verifier, challenge = _pkce()
    code = await core.create_authorization_code(
        redis_client,
        client_id=CLIENT,
        redirect_uri=REDIRECT,
        code_challenge=challenge,
        user_id=boot.user_id,
        organization_id=boot.organization_id,
        scope="knowledge:read",
    )
    await db_session.commit()
    other_verifier, _ = _pkce()
    with pytest.raises(core.OAuthError):
        await core.exchange_code(
            db_session, redis_client, code=code, code_verifier=other_verifier,
            redirect_uri=REDIRECT, client_id=CLIENT, client_name="x",
        )
    # A thief who guessed wrong cannot retry; and the real tool is told to restart.
    with pytest.raises(core.OAuthError):
        await core.exchange_code(
            db_session, redis_client, code=code, code_verifier=verifier,
            redirect_uri=REDIRECT, client_id=CLIENT, client_name="x",
        )


@pytest.mark.parametrize("field", ["redirect_uri", "client_id"])
async def test_a_code_is_bound_to_its_redirect_and_client(db_session, redis_client, field):
    boot = await _org(db_session, enabled=True)
    verifier, challenge = _pkce()
    code = await core.create_authorization_code(
        redis_client,
        client_id=CLIENT,
        redirect_uri=REDIRECT,
        code_challenge=challenge,
        user_id=boot.user_id,
        organization_id=boot.organization_id,
        scope="knowledge:read",
    )
    await db_session.commit()
    args = dict(
        code=code, code_verifier=verifier, redirect_uri=REDIRECT,
        client_id=CLIENT, client_name="x",
    )
    args[field] = (
        "http://127.0.0.1:60000/callback" if field == "redirect_uri" else "other"
    )
    with pytest.raises(core.OAuthError):
        await core.exchange_code(db_session, redis_client, **args)


async def test_an_expired_or_made_up_code_is_refused(db_session, redis_client):
    verifier, _ = _pkce()
    for code in ("qbr_ac_nonsense", "nonsense", ""):
        with pytest.raises(core.OAuthError):
            await core.exchange_code(
                db_session, redis_client, code=code, code_verifier=verifier,
                redirect_uri=REDIRECT, client_id=CLIENT, client_name="x",
            )


# ---------------------------------------------------------------------------
# The organisation switch
# ---------------------------------------------------------------------------


async def test_outside_tools_are_off_by_default(db_session):
    await db_session.commit()
    unique = uuid.uuid4().hex[:8]
    boot = await onboarding.bootstrap_organization(
        db_session,
        name=f"New {unique}",
        slug=f"new-{unique}",
        owner_email=f"n-{unique}@example.com",
        owner_display_name="N",
        tier="team",
    )
    org = await db_session.get(Organization, boot.organization_id)
    assert org.allow_external_tools is False


async def test_no_grant_is_created_while_the_switch_is_off(db_session, redis_client):
    boot = await _org(db_session, enabled=False)
    with pytest.raises(core.OAuthError) as exc:
        await _tokens(db_session, redis_client, boot)
    assert _error_code(exc) == "access_denied"


async def test_turning_the_switch_off_cuts_access_at_once(db_session, redis_client):
    boot = await _org(db_session, enabled=True)
    issued = await _tokens(db_session, redis_client, boot)
    await db_session.commit()
    ext = await core.authenticate_access_token(db_session, issued.access_token)
    assert ext.user.id == boot.user_id

    org = await db_session.get(Organization, boot.organization_id)
    org.allow_external_tools = False
    await db_session.flush()
    await db_session.commit()
    with pytest.raises(core.ExternalAuthError) as exc:
        await core.authenticate_access_token(db_session, issued.access_token)
    assert exc.value.code == "external_tools_disabled"
    assert exc.value.status == 403
    # Refreshing does not get around it either.
    await db_session.commit()
    with pytest.raises(core.OAuthError):
        await core.refresh_tokens(
            db_session, refresh_token=issued.refresh_token, client_id=CLIENT
        )

    # Switching it back on restores access; the grant itself was never revoked.
    await db_session.commit()
    org = await db_session.get(Organization, boot.organization_id)
    org.allow_external_tools = True
    await db_session.flush()
    await db_session.commit()
    again = await core.authenticate_access_token(db_session, issued.access_token)
    assert again.user.id == boot.user_id


async def test_a_suspended_person_loses_access(db_session, redis_client):
    boot = await _org(db_session, enabled=True)
    issued = await _tokens(db_session, redis_client, boot)
    user = await db_session.get(User, boot.user_id)
    user.status = "suspended"
    await db_session.flush()
    await db_session.commit()
    with pytest.raises(core.ExternalAuthError) as exc:
        await core.authenticate_access_token(db_session, issued.access_token)
    assert exc.value.code == "invalid_token"


# ---------------------------------------------------------------------------
# Tokens: rotation, reuse, expiry, revocation
# ---------------------------------------------------------------------------


async def test_refresh_rotates_both_tokens(db_session, redis_client):
    boot = await _org(db_session, enabled=True)
    first = await _tokens(db_session, redis_client, boot)
    await db_session.commit()
    second = await core.refresh_tokens(
        db_session, refresh_token=first.refresh_token, client_id=CLIENT
    )
    assert second.access_token != first.access_token
    assert second.refresh_token != first.refresh_token
    assert second.grant_id == first.grant_id
    await db_session.commit()
    with pytest.raises(core.ExternalAuthError):
        await core.authenticate_access_token(db_session, first.access_token)
    ext = await core.authenticate_access_token(db_session, second.access_token)
    assert ext.user.id == boot.user_id


async def test_a_refresh_token_used_twice_revokes_the_grant(db_session, redis_client):
    boot = await _org(db_session, enabled=True)
    first = await _tokens(db_session, redis_client, boot)
    await db_session.commit()
    second = await core.refresh_tokens(
        db_session, refresh_token=first.refresh_token, client_id=CLIENT
    )
    await db_session.commit()

    # Someone replays the old refresh token.
    with pytest.raises(core.OAuthError):
        await core.refresh_tokens(
            db_session, refresh_token=first.refresh_token, client_id=CLIENT
        )
    await db_session.commit()
    # ...and the newest tokens stop working too, and the revoke was recorded.
    with pytest.raises(core.ExternalAuthError):
        await core.authenticate_access_token(db_session, second.access_token)
    await db_session.commit()
    with pytest.raises(core.OAuthError):
        await core.refresh_tokens(
            db_session, refresh_token=second.refresh_token, client_id=CLIENT
        )
    grant = await _grant(db_session, first.grant_id)
    assert grant.revoke_reason == "refresh_reuse"
    events = (
        await db_session.execute(
            select(AuditEvent.action, AuditEvent.actor_type).where(
                AuditEvent.organization_id == boot.organization_id,
                AuditEvent.resource_id == first.grant_id,
            )
        )
    ).all()
    assert ("external_access.revoked", "system") in events
    assert ("external_access.granted", "user") in events


async def test_a_refresh_token_belongs_to_one_client(db_session, redis_client):
    boot = await _org(db_session, enabled=True)
    issued = await _tokens(db_session, redis_client, boot)
    await db_session.commit()
    with pytest.raises(core.OAuthError):
        await core.refresh_tokens(
            db_session, refresh_token=issued.refresh_token, client_id="other"
        )


async def test_expired_and_malformed_tokens_are_refused(db_session, redis_client):
    boot = await _org(db_session, enabled=True)
    issued = await _tokens(db_session, redis_client, boot)
    grant = await _grant(db_session, issued.grant_id)

    grant.access_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.flush()
    await db_session.commit()
    with pytest.raises(core.ExternalAuthError):
        await core.authenticate_access_token(db_session, issued.access_token)

    for bad in ("", "nonsense", "qbr_at_", "qbr_at_" + "a" * 500, issued.refresh_token):
        await db_session.commit()
        with pytest.raises(core.ExternalAuthError):
            await core.authenticate_access_token(db_session, bad)


async def test_refresh_never_extends_a_grant_past_its_hard_limit(db_session, redis_client):
    boot = await _org(db_session, enabled=True)
    issued = await _tokens(db_session, redis_client, boot)
    grant = await _grant(db_session, issued.grant_id)
    grant.absolute_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.flush()
    await db_session.commit()
    with pytest.raises(core.OAuthError):
        await core.refresh_tokens(
            db_session, refresh_token=issued.refresh_token, client_id=CLIENT
        )


async def test_revoking_works_by_the_tool_and_by_the_person(db_session, redis_client):
    boot = await _org(db_session, enabled=True)
    one = await _tokens(db_session, redis_client, boot)
    await db_session.commit()
    await core.revoke_by_token(db_session, one.refresh_token)
    await db_session.commit()
    with pytest.raises(core.ExternalAuthError):
        await core.authenticate_access_token(db_session, one.access_token)

    two = await _tokens(db_session, redis_client, boot)
    await db_session.commit()
    user = await db_session.get(User, boot.user_id)
    await db_session.commit()
    # Someone else's identity cannot revoke it...
    assert not await core.revoke_grant_for_user(
        db_session, grant_id=two.grant_id, identity_id=uuid.uuid4()
    )
    await db_session.commit()
    await core.authenticate_access_token(db_session, two.access_token)
    await db_session.commit()
    # ...the person who granted it can.
    assert await core.revoke_grant_for_user(
        db_session, grant_id=two.grant_id, identity_id=user.identity_id
    )
    await db_session.commit()
    with pytest.raises(core.ExternalAuthError):
        await core.authenticate_access_token(db_session, two.access_token)


async def test_rate_limit_stops_a_flood_and_fails_closed(redis_client):
    key = f"quberi:oauth:test:{uuid.uuid4().hex}"
    for _ in range(3):
        await core.rate_limit(redis_client, key=key, limit=3, window_seconds=60)
    with pytest.raises(core.OAuthError) as exc:
        await core.rate_limit(redis_client, key=key, limit=3, window_seconds=60)
    assert exc.value.status == 429

    dead = Redis.from_url("redis://127.0.0.1:1/0")
    with pytest.raises(core.OAuthError) as exc:
        await core.rate_limit(dead, key=key, limit=3, window_seconds=60)
    assert exc.value.status == 503


# ---------------------------------------------------------------------------
# HTTP layer (mini app: consent + token + search)
# ---------------------------------------------------------------------------


@pytest.fixture
async def http(db_session, redis_client):
    app = FastAPI()
    app.state.settings = get_settings()
    app.include_router(oauth_router, prefix="/api/v1")
    app.include_router(external_router, prefix="/api/v1")
    app.include_router(me_external_access_router, prefix="/api/v1")
    app.include_router(well_known_router)

    infra = SimpleNamespace(redis=redis_client, qdrant=object())

    async def _session():
        yield db_session

    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[dependencies.get_infrastructure] = lambda: infra
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _login(db, boot) -> dict:
    user = await db.get(User, boot.user_id)
    await db.commit()
    token = issue_session(
        identity_id=user.identity_id,
        user_id=user.id,
        organization_id=boot.organization_id,
        email=user.email,
    )
    return {"Authorization": f"Bearer {token}"}


async def _authorize(http, headers, boot, *, state="st8", challenge=None):
    verifier, ch = _pkce()
    resp = await http.post(
        "/api/v1/oauth/authorize",
        headers=headers,
        json={
            "client_id": CLIENT,
            "redirect_uri": REDIRECT,
            "state": state,
            "code_challenge": challenge or ch,
            "code_challenge_method": "S256",
            "organization_id": str(boot.organization_id),
        },
    )
    return resp, verifier


async def test_full_login_over_http_then_search_as_the_granting_person(
    http, db_session, redis_client, monkeypatch
):
    boot = await _org(db_session, enabled=True)
    headers = await _login(db_session, boot)

    consent = await http.get(
        "/api/v1/oauth/consent",
        headers=headers,
        params={"client_id": CLIENT, "redirect_uri": REDIRECT},
    )
    assert consent.status_code == 200
    body = consent.json()
    assert body["client_name"] == "DeepSeek Harness"
    assert body["scope"] == "knowledge:read"
    assert [o["external_tools_enabled"] for o in body["organizations"]] == [True]

    resp, verifier = await _authorize(http, headers, boot)
    assert resp.status_code == 200, resp.text
    target = urlsplit(resp.json()["redirect_to"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == REDIRECT
    query = parse_qs(target.query)
    assert query["state"] == ["st8"]

    tok = await http.post(
        "/api/v1/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "code_verifier": verifier,
            "redirect_uri": REDIRECT,
            "client_id": CLIENT,
        },
    )
    assert tok.status_code == 200, tok.text
    assert tok.headers["cache-control"] == "no-store"
    tokens = tok.json()
    assert tokens["token_type"] == "Bearer"

    # Search: whatever the tool sends, it runs as the person and organisation of
    # the grant. Retrieval is stubbed; we check who it was asked on behalf of.
    seen = {}
    doc, space = uuid.uuid4(), uuid.uuid4()

    async def fake_retrieve(session, *, qdrant, organization_id, caller_user_id, query, top_k):
        seen.update(org=organization_id, user=caller_user_id, query=query, top_k=top_k)
        chunk = SimpleNamespace(
            document_id=doc, space_id=space, document_name="Handbook.md",
            chunk_index=0, text="Remote work is allowed.", score=0.9,
        )
        return SimpleNamespace(chunks=[chunk], space_ids=[space])

    monkeypatch.setattr(external_api, "retrieve_for_user", fake_retrieve)
    other_org = uuid.uuid4()
    search = await http.post(
        "/api/v1/external/search",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json={"query": "remote work", "top_k": 3},
    )
    assert search.status_code == 200, search.text
    assert seen["org"] == boot.organization_id
    assert seen["user"] == boot.user_id
    assert seen["top_k"] == 3
    assert search.json()["passages"][0]["document_name"] == "Handbook.md"

    # A tool cannot name another user or organisation: unknown fields are refused.
    forged = await http.post(
        "/api/v1/external/search",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json={"query": "x", "organization_id": str(other_org)},
    )
    assert forged.status_code == 422

    # The read was audited with the tool's name and document ids, not the text.
    await db_session.commit()
    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.organization_id == boot.organization_id,
                AuditEvent.action == "knowledge.external_search",
            )
        )
    ).scalar_one()
    assert event.actor_id == boot.user_id
    assert event.payload["client_name"] == "DeepSeek Harness"
    assert event.payload["retrieved_document_ids"] == [str(doc)]
    assert "remote work" not in str(event.payload)
    assert "Remote work is allowed." not in str(event.payload)


async def test_search_needs_a_valid_token_and_the_switch(http, db_session, redis_client):
    boot = await _org(db_session, enabled=True)
    issued = await _tokens(db_session, redis_client, boot)
    await db_session.commit()

    none = await http.post("/api/v1/external/search", json={"query": "x"})
    assert none.status_code == 401
    assert "invalid_token" in none.headers["www-authenticate"]
    bad = await http.post(
        "/api/v1/external/search",
        headers={"Authorization": "Bearer qbr_at_nonsense"},
        json={"query": "x"},
    )
    assert bad.status_code == 401
    # A web session token is not an access token.
    web = await http.post(
        "/api/v1/external/search", headers=await _login(db_session, boot), json={"query": "x"}
    )
    assert web.status_code == 401

    org = await db_session.get(Organization, boot.organization_id)
    org.allow_external_tools = False
    await db_session.flush()
    await db_session.commit()
    off = await http.post(
        "/api/v1/external/search",
        headers={"Authorization": f"Bearer {issued.access_token}"},
        json={"query": "x"},
    )
    assert off.status_code == 403
    assert off.json()["detail"]["code"] == "external_tools_disabled"


async def test_search_is_rate_limited_per_grant(http, db_session, redis_client, monkeypatch):
    boot = await _org(db_session, enabled=True)
    issued = await _tokens(db_session, redis_client, boot)
    await db_session.commit()

    async def fake_retrieve(session, **kwargs):
        return SimpleNamespace(chunks=[], space_ids=[])

    monkeypatch.setattr(external_api, "retrieve_for_user", fake_retrieve)
    monkeypatch.setattr(external_api, "_SEARCH_RATE_LIMIT", 2)
    headers = {"Authorization": f"Bearer {issued.access_token}"}
    codes = []
    for _ in range(3):
        resp = await http.post("/api/v1/external/search", headers=headers, json={"query": "x"})
        codes.append(resp.status_code)
        await db_session.commit()
    assert codes == [200, 200, 429]


async def test_consent_refuses_guests_unknown_apps_and_bad_redirects(http, db_session):
    boot = await _org(db_session, enabled=True)
    headers = await _login(db_session, boot)
    user = await db_session.get(User, boot.user_id)
    guest = issue_session(
        identity_id=user.identity_id, user_id=user.id,
        organization_id=boot.organization_id, email=user.email, is_guest=True,
    )

    base = {"client_id": CLIENT, "redirect_uri": REDIRECT}
    ok = await http.get("/api/v1/oauth/consent", headers=headers, params=base)
    assert ok.status_code == 200
    assert (
        await http.get(
            "/api/v1/oauth/consent",
            headers={"Authorization": f"Bearer {guest}"},
            params=base,
        )
    ).status_code == 403
    assert (await http.get("/api/v1/oauth/consent", params=base)).status_code == 401
    for params in (
        {**base, "client_id": "unknown"},
        {**base, "redirect_uri": "https://evil.example/callback"},
        {**base, "redirect_uri": "http://localhost:5000/callback"},
        {**base, "scope": "admin"},
    ):
        resp = await http.get("/api/v1/oauth/consent", headers=headers, params=params)
        assert resp.status_code == 400, params


async def test_authorize_needs_membership_pkce_and_the_switch(http, db_session):
    boot = await _org(db_session, enabled=True)
    stranger = await _org(db_session, enabled=True)
    headers = await _login(db_session, boot)

    # Not a member of that organisation.
    resp, _ = await _authorize(http, headers, stranger)
    assert resp.status_code == 403

    # PKCE is mandatory and only S256.
    verifier, challenge = _pkce()
    payload = {
        "client_id": CLIENT, "redirect_uri": REDIRECT,
        "organization_id": str(boot.organization_id),
    }
    for extra in (
        {"code_challenge": challenge, "code_challenge_method": "plain"},
        {"code_challenge": "too-short", "code_challenge_method": "S256"},
    ):
        resp = await http.post(
            "/api/v1/oauth/authorize", headers=headers, json={**payload, **extra}
        )
        assert resp.status_code == 400, extra
    missing = await http.post("/api/v1/oauth/authorize", headers=headers, json=payload)
    assert missing.status_code == 422

    # Switch off: no code at all.
    org = await db_session.get(Organization, boot.organization_id)
    org.allow_external_tools = False
    await db_session.flush()
    await db_session.commit()
    resp, _ = await _authorize(http, headers, boot)
    assert resp.status_code == 403


async def test_owner_can_switch_on_from_the_consent_page_but_a_member_cannot(
    http, db_session
):
    boot = await _org(db_session, enabled=False)
    headers = await _login(db_session, boot)
    consent = await http.get(
        "/api/v1/oauth/consent",
        headers=headers,
        params={"client_id": CLIENT, "redirect_uri": REDIRECT},
    )
    org_view = consent.json()["organizations"][0]
    assert org_view["external_tools_enabled"] is False
    assert org_view["can_enable"] is True

    # A plain member of the same organisation is told they cannot.
    from quberi_api.services import members as members_service

    await db_session.commit()
    invited = await members_service.invite_member(
        db_session,
        organization_id=boot.organization_id,
        invited_by_user_id=boot.user_id,
        email=f"m-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Member",
        role_code="member",
    )
    member = await db_session.get(User, invited.user.id)
    member.status = "active"
    await db_session.flush()
    await db_session.commit()
    member_headers = {
        "Authorization": "Bearer "
        + issue_session(
            identity_id=member.identity_id, user_id=member.id,
            organization_id=boot.organization_id, email=member.email,
        )
    }
    seen = await http.get(
        "/api/v1/oauth/consent",
        headers=member_headers,
        params={"client_id": CLIENT, "redirect_uri": REDIRECT},
    )
    assert seen.json()["organizations"][0]["can_enable"] is False
    denied = await http.post(
        "/api/v1/oauth/enable-external-tools",
        headers=member_headers,
        json={"organization_id": str(boot.organization_id)},
    )
    assert denied.status_code == 403
    await db_session.commit()
    org = await db_session.get(Organization, boot.organization_id)
    assert org.allow_external_tools is False

    ok = await http.post(
        "/api/v1/oauth/enable-external-tools",
        headers=headers,
        json={"organization_id": str(boot.organization_id)},
    )
    assert ok.status_code == 204
    await db_session.commit()
    org = await db_session.get(Organization, boot.organization_id)
    assert org.allow_external_tools is True
    audited = (
        await db_session.execute(
            select(AuditEvent.payload).where(
                AuditEvent.organization_id == boot.organization_id,
                AuditEvent.action == "organization.external_tools_changed",
            )
        )
    ).scalar_one()
    assert audited == {"enabled": True}


async def test_token_endpoint_errors_follow_oauth_and_leak_nothing(http, db_session):
    base = {"client_id": CLIENT}
    cases = [
        ({"grant_type": "password", **base}, 400, "unsupported_grant_type"),
        ({"grant_type": "authorization_code", "client_id": "nope"}, 401, "invalid_client"),
        (
            {"grant_type": "authorization_code", "code": "qbr_ac_x",
             "code_verifier": "a" * 50, "redirect_uri": REDIRECT, **base},
            400, "invalid_grant",
        ),
        (
            {"grant_type": "refresh_token", "refresh_token": "qbr_rt_x", **base},
            400,
            "invalid_grant",
        ),
    ]
    for data, status, error in cases:
        resp = await http.post("/api/v1/oauth/token", data=data)
        assert resp.status_code == status, (data, resp.text)
        assert resp.json()["error"] == error
        assert resp.headers["cache-control"] == "no-store"


async def test_revoke_always_answers_200(http, db_session, redis_client):
    boot = await _org(db_session, enabled=True)
    issued = await _tokens(db_session, redis_client, boot)
    await db_session.commit()
    for token in (issued.refresh_token, "qbr_at_unknown", ""):
        resp = await http.post("/api/v1/oauth/revoke", data={"token": token})
        assert resp.status_code == 200
    await db_session.commit()
    with pytest.raises(core.ExternalAuthError):
        await core.authenticate_access_token(db_session, issued.access_token)


async def test_a_person_sees_and_disconnects_their_tools(http, db_session, redis_client):
    boot = await _org(db_session, enabled=True)
    other = await _org(db_session, enabled=True)
    mine = await _tokens(db_session, redis_client, boot)
    theirs = await _tokens(db_session, redis_client, other)
    headers = await _login(db_session, boot)

    listed = await http.get("/api/v1/me/external-access", headers=headers)
    ids = [g["id"] for g in listed.json()["items"]]
    assert ids == [str(mine.grant_id)]
    assert listed.json()["items"][0]["client_name"] == "DeepSeek Harness"
    assert "access_token" not in listed.text and "qbr_" not in listed.text

    # Someone else's grant is invisible and cannot be revoked.
    miss = await http.delete(f"/api/v1/me/external-access/{theirs.grant_id}", headers=headers)
    assert miss.status_code == 404
    await db_session.commit()
    await core.authenticate_access_token(db_session, theirs.access_token)

    await db_session.commit()
    gone = await http.delete(f"/api/v1/me/external-access/{mine.grant_id}", headers=headers)
    assert gone.status_code == 204
    await db_session.commit()
    with pytest.raises(core.ExternalAuthError):
        await core.authenticate_access_token(db_session, mine.access_token)
    after = await http.get("/api/v1/me/external-access", headers=headers)
    assert after.json()["items"] == []


async def test_discovery_document_points_at_the_right_places(http):
    resp = await http.get("/.well-known/oauth-authorization-server")
    meta = resp.json()
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]
    assert meta["token_endpoint"].endswith("/api/v1/oauth/token")
    assert meta["authorization_endpoint"].endswith("/oauth/authorize")
    assert meta["scopes_supported"] == [
        "knowledge:read",
        "knowledge:write",
        "knowledge:upload",
    ]
