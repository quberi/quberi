# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""MCP tools must see the same permission boundaries the REST external API does.

``search`` dispatches into ``retrieve_for_user`` (adapters.do_search), the exact
function test_frozen_search_topk.py already proves correctly excludes a frozen
personal space from the vector search itself (not a post-hoc filter — see that
module's docstring). This file does not re-prove that mechanism; it proves the
MCP *entry point* reaches the same adapter function and therefore inherits the
same guarantee, by calling the MCP endpoint instead of the service function
directly and checking that a frozen member's content disappears from — and a
shared space's content remains in — the results.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import update

from quberi_api.api import dependencies
from quberi_api.api.v1.mcp.router import mcp_router
from quberi_api.core.config import get_settings
from quberi_api.domain.documents.models import Document
from quberi_api.services.oauth import core
from quberi_api.services.rag import retrieve
from quberi_api.services.space_freeze import freeze_personal_spaces
from tests.test_document_acl import TEXT_DIM, TEXT_MODEL, _member, _text_store
from tests.test_external_save import _org
from tests.test_frozen_search_topk import personal_space_of
from tests.test_image_indexing import _doc
from tests.test_image_indexing import _space as _make_space
from tests.test_image_search import _grant
from tests.test_mcp_handshake import _tokens as _mcp_signed_tokens  # noqa: F401

MCP_CLIENT = "quberi-mcp"


@pytest.fixture
async def qdrant():
    from qdrant_client import AsyncQdrantClient

    client = AsyncQdrantClient(":memory:")
    yield client
    await client.close()


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    class _Embedder:
        vector_dim = TEXT_DIM

        async def embed(self, texts, **_kw):
            return [[1.0] + [0.0] * (TEXT_DIM - 1) for _ in texts]

    async def _resolve(_session, *, organization_id):
        return SimpleNamespace(
            provider=_Embedder(), model=SimpleNamespace(model_id=TEXT_MODEL, vector_dim=TEXT_DIM)
        )

    monkeypatch.setattr(retrieve, "resolve_embedding_provider", _resolve)


@pytest.fixture
async def redis_client():
    client = Redis.from_url("redis://127.0.0.1:6379/1", decode_responses=True)
    for pattern in ("quberi:oauth:*", "quberi:external:*"):
        for key in await client.keys(pattern):
            await client.delete(key)
    yield client
    await client.aclose()


@pytest.fixture
async def http(db_session, redis_client, qdrant):
    app = FastAPI()
    app.state.settings = get_settings()
    app.include_router(mcp_router)
    infra = SimpleNamespace(redis=redis_client, qdrant=qdrant)

    async def _session():
        yield db_session

    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[dependencies.get_infrastructure] = lambda: infra
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _pkce() -> tuple[str, str]:
    verifier = (
        base64.urlsafe_b64encode(uuid.uuid4().bytes + uuid.uuid4().bytes).rstrip(b"=").decode()
    )
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


REDIRECT = "http://127.0.0.1:53682/callback"


async def _tokens(db, redis, boot, *, scope, user_id):
    verifier, challenge = _pkce()
    code = await core.create_authorization_code(
        redis,
        client_id=MCP_CLIENT,
        redirect_uri=REDIRECT,
        code_challenge=challenge,
        user_id=user_id,
        organization_id=boot.organization_id,
        scope=scope,
    )
    await db.commit()
    return await core.exchange_code(
        db, redis, code=code, code_verifier=verifier, redirect_uri=REDIRECT,
        client_id=MCP_CLIENT, client_name="MCP client",
    )


async def _passage(db, boot, qdrant, space, name, *, close):
    from quberi_api.services.rag.vector_store import ChunkPoint

    doc = await _doc(db, boot, space, name, mime="application/pdf", state="indexed")
    vector = [1.0] + [0.0] * (TEXT_DIM - 1) if close else [0.6, 0.8] + [0.0] * (TEXT_DIM - 2)
    await _text_store(qdrant).upsert_document(
        organization_id=boot.organization_id,
        space_id=space.id,
        document_id=doc.id,
        document_name=name,
        document_checksum=None,
        chunks=[
            ChunkPoint(
                chunk_index=0, text=f"passage of {name}", vector=vector, char_start=0, char_end=10
            )
        ],
    )
    await db.execute(
        update(Document).where(Document.id == doc.id).values(indexing_state="indexed")
    )
    await db.commit()
    return doc


async def test_a_frozen_members_personal_content_disappears_from_mcp_search(
    http, db_session, redis_client, qdrant
):
    boot = await _org(db_session)
    person = await _member(db_session, boot)
    personal = SimpleNamespace(id=await personal_space_of(db_session, person.id))
    shared_made = await _make_space(db_session, boot, "Shared")
    shared = SimpleNamespace(id=shared_made.id)
    await _grant(db_session, boot, shared, SimpleNamespace(id=person.id), "viewer")
    await _passage(db_session, boot, qdrant, personal, "private-note.pdf", close=True)
    await _passage(db_session, boot, qdrant, shared, "shared-doc.pdf", close=True)

    tokens = await _tokens(
        db_session, redis_client, boot, scope="knowledge:read", user_id=person.id
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {tokens.access_token}"}
    search_call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "search", "arguments": {"query": "passage"}},
    }

    before = await http.post("/", headers=headers, json=search_call)
    before_result = before.json()["result"]["structuredContent"]
    before_names = {p["document_name"] for p in before_result["passages"]}
    assert before_names == {"private-note.pdf", "shared-doc.pdf"}

    await db_session.commit()
    async with db_session.begin():
        await freeze_personal_spaces(
            db_session,
            organization_id=boot.organization_id,
            owner_user_id=person.id,
            actor_user_id=boot.user_id,
        )

    search_call["id"] = 2
    after = await http.post("/", headers=headers, json=search_call)
    after_result = after.json()["result"]["structuredContent"]
    after_names = {p["document_name"] for p in after_result["passages"]}
    assert after_names == {"shared-doc.pdf"}
    assert "private-note.pdf" not in after.text


async def test_a_frozen_personal_space_is_not_listed_by_mcp_list_spaces(
    http, db_session, redis_client, qdrant
):
    boot = await _org(db_session)
    person = await _member(db_session, boot)
    personal_id = await personal_space_of(db_session, person.id)

    tokens = await _tokens(
        db_session, redis_client, boot, scope="knowledge:read", user_id=person.id
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {tokens.access_token}"}
    list_call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "list_spaces", "arguments": {}},
    }

    before = await http.post("/", headers=headers, json=list_call)
    assert before.json()["result"]["structuredContent"]["total"] >= 1

    await db_session.commit()
    async with db_session.begin():
        await freeze_personal_spaces(
            db_session,
            organization_id=boot.organization_id,
            owner_user_id=person.id,
            actor_user_id=boot.user_id,
        )

    list_call["id"] = 2
    after = await http.post("/", headers=headers, json=list_call)
    assert after.json()["result"]["structuredContent"]["total"] == 0
    _ = personal_id
