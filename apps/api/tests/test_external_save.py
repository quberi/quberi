"""Saving a note through the external-tools API (the write side).

``POST /api/v1/external/save`` is the write counterpart of
``/api/v1/external/search``. It uses the same OAuth access-token path, but
requires the ``knowledge:write`` scope and lands a markdown note into a space
the grant's owner can write to (named, or their personal default).

What these tests pin:

* a grant without ``knowledge:write`` is refused (``insufficient_scope``),
  even though it could search;
* a ``knowledge:write`` grant saves into the personal space by default, and
  into a named space when asked;
* the note runs as the grant's own person (editor+ is re-checked), so a space
  the person cannot write to is a clean 403;
* an unknown space name is a 404;
* the write is audited with the tool's name and the new document id.

The MinIO store and the index enqueue that ``save_note`` triggers are the two
pieces of infrastructure the test environment doesn't run, so both are
monkeypatched; everything else runs for real against the test database.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import select

from quberi_api.api import dependencies
from quberi_api.api.v1.external import external_router
from quberi_api.core.config import get_settings
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.identity.models import Organization, User
from quberi_api.services import members as members_service
from quberi_api.services import onboarding
from quberi_api.services import space_bindings as space_bindings_service
from quberi_api.services import spaces as spaces_service
from quberi_api.services.documents import core as documents_core
from quberi_api.services.oauth import core

REDIRECT = "http://127.0.0.1:53682/callback"
CLIENT = "quberi-harness"


def _pkce() -> tuple[str, str]:
    verifier = (
        base64.urlsafe_b64encode(uuid.uuid4().bytes + uuid.uuid4().bytes)
        .rstrip(b"=")
        .decode()
    )
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
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


async def _tokens(db, redis, boot, *, scope, user_id=None, client_id=CLIENT):
    # client_id defaults to CLIENT ("quberi-harness") so every existing call
    # site is unaffected; a few MCP-specific tests (e.g.
    # test_oauth_member_revocation.py's quberi-mcp regression) pass "quberi-mcp".
    client_name = "MCP client" if client_id == "quberi-mcp" else "DeepSeek Harness"
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
        client_name=client_name,
    )


@dataclass
class _StoreResult:
    storage_key: str
    size_bytes: int
    checksum_sha256: str


@pytest.fixture
def _no_infra(monkeypatch):
    """Stub the two infra calls save_note makes (MinIO store, index enqueue)."""

    captured: dict = {}

    async def fake_store(*, storage_key, source_stream, content_type, max_bytes):
        blob = source_stream.read()
        captured["blob"] = blob
        captured["content_type"] = content_type
        return _StoreResult(storage_key=storage_key, size_bytes=len(blob), checksum_sha256=hashlib.sha256(blob).hexdigest())

    async def fake_enqueue(document_id):
        captured["enqueued"] = document_id
        return "job-test"

    monkeypatch.setattr(documents_core.local_connector, "store", fake_store)
    monkeypatch.setattr(documents_core, "enqueue_index_document", fake_enqueue)
    return captured


@pytest.fixture
async def http(db_session, redis_client):
    app = FastAPI()
    app.state.settings = get_settings()
    app.include_router(external_router, prefix="/api/v1")
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


# ---------------------------------------------------------------------------
# Scope gate
# ---------------------------------------------------------------------------


async def test_a_read_only_grant_cannot_save(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    read_only = await _tokens(db_session, redis_client, boot, scope="knowledge:read")
    await db_session.commit()
    resp = await http.post(
        "/api/v1/external/save",
        headers=_auth(read_only),
        json={"title": "x", "text": "y"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "insufficient_scope"


# ---------------------------------------------------------------------------
# Happy path: default (personal) space and named space
# ---------------------------------------------------------------------------


async def test_write_grant_saves_into_personal_space_by_default(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    tokens = await _tokens(
        db_session, redis_client, boot, scope="knowledge:read knowledge:write"
    )
    await db_session.commit()
    resp = await http.post(
        "/api/v1/external/save",
        headers=_auth(tokens),
        json={"title": "会议结论", "text": "决定采用方案 B。"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["space_id"] == str(boot.personal_space_id)
    assert body["name"] == "会议结论.md"
    # The composed markdown actually reached the (stubbed) store.
    assert "决定采用方案 B。" in _no_infra["blob"].decode("utf-8")


async def test_write_grant_saves_into_a_named_space(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    # Owner creates a collaborative space they own (so editor+ holds).
    view = await spaces_service.create_collaborative_space(
        db_session,
        organization_id=boot.organization_id,
        creator_user_id=boot.user_id,
        name="产品团队",
        slug=f"pt-{uuid.uuid4().hex[:8]}",
    )
    await db_session.commit()
    tokens = await _tokens(
        db_session, redis_client, boot, scope="knowledge:read knowledge:write"
    )
    await db_session.commit()
    resp = await http.post(
        "/api/v1/external/save",
        headers=_auth(tokens),
        json={"title": "note", "text": "body", "space": "产品团队"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["space_id"] == str(view.space.id)


# ---------------------------------------------------------------------------
# Permission + not-found
# ---------------------------------------------------------------------------


async def test_unknown_space_name_is_404(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    tokens = await _tokens(
        db_session, redis_client, boot, scope="knowledge:read knowledge:write"
    )
    await db_session.commit()
    resp = await http.post(
        "/api/v1/external/save",
        headers=_auth(tokens),
        json={"title": "x", "text": "y", "space": "no-such-space"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "space_not_found"


async def test_viewer_only_space_is_403(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    # A space owned by the organisation; the member is granted viewer only.
    view = await spaces_service.create_collaborative_space(
        db_session,
        organization_id=boot.organization_id,
        creator_user_id=boot.user_id,
        name="只读区",
        slug=f"ro-{uuid.uuid4().hex[:8]}",
        owner="organization",
    )
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
    await space_bindings_service.add_binding(
        db_session,
        space_id=view.space.id,
        organization_id=boot.organization_id,
        actor_user_id=boot.user_id,
        subject_type="user",
        subject_id=member.id,
        role_code="viewer",
    )
    await db_session.commit()
    tokens = await _tokens(
        db_session,
        redis_client,
        boot,
        scope="knowledge:read knowledge:write",
        user_id=member.id,
    )
    await db_session.commit()
    resp = await http.post(
        "/api/v1/external/save",
        headers=_auth(tokens),
        json={"title": "x", "text": "y", "space": "只读区"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


async def test_save_is_audited(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    tokens = await _tokens(
        db_session, redis_client, boot, scope="knowledge:read knowledge:write"
    )
    await db_session.commit()
    resp = await http.post(
        "/api/v1/external/save",
        headers=_auth(tokens),
        json={"title": "t", "text": "b"},
    )
    assert resp.status_code == 201
    await db_session.commit()
    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.organization_id == boot.organization_id,
                AuditEvent.action == "knowledge.external_save",
            )
        )
    ).scalar_one()
    assert event.actor_id == boot.user_id
    assert event.payload["client_name"] == "DeepSeek Harness"
    assert event.payload["document_id"] == resp.json()["document_id"]
