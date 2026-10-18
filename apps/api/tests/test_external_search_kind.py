# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""``POST /external/search`` looks in the text, the pictures, or both.

By default (``both``) a text search also offers the few pictures that might be what was asked
for, beside the passages: a tool that only searched text would say "nothing found" about a
picture that exists. ``text`` keeps the old behaviour; ``image`` searches pictures only.

Real: database, permission evaluator, picture vector store (qdrant in memory), OAuth grant.
Stood in: the picture model, the file store and the text retrieval.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from quberi_api.api.v1 import external as external_api
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.services.doc_acl import NO_DOC_ACCESS
from quberi_api.services.rag import image_search
from quberi_api.services.rag.vector_store import RetrievedChunk
from tests.test_external_image_search import (  # noqa: F401
    _auth,
    _member,
    _grant,
    _org,
    _read,
    _stored,
    blobs,
    fake_embedder,
    http,
    qdrant,
    redis_client,
    signing_key,
)
from tests.test_image_indexing import _space as _make_space
from tests.test_image_search import BLUE, RED

SEARCH = "/api/v1/external/search"


@pytest.fixture
def text_side(monkeypatch):
    """Text retrieval stood in for: returns whatever ``state.chunks`` holds."""
    state = SimpleNamespace(chunks=[], spaces=[], calls=0)

    async def fake_retrieve(
        session, *, qdrant, organization_id, caller_user_id, query, top_k, space_ids=None
    ):
        state.calls += 1
        return SimpleNamespace(
            chunks=state.chunks, space_ids=state.spaces, doc_access=NO_DOC_ACCESS
        )

    monkeypatch.setattr(external_api, "retrieve_for_user", fake_retrieve)
    return state


@pytest.fixture
def embed_calls(monkeypatch):
    calls: list[dict] = []
    original = image_search.embed_query

    async def spy(**kwargs):
        calls.append(kwargs)
        return await original(**kwargs)

    monkeypatch.setattr(image_search, "embed_query", spy)
    return calls


async def _ask(http, db, redis, boot, body, **kw):
    tokens = await _read(db, redis, boot, **kw)
    return await http.post(SEARCH, headers=_auth(tokens), json={"query": "动物母狮", **body})


async def test_a_text_search_offers_pictures_beside_the_passages_by_default(
    http, db_session, redis_client, qdrant, blobs, text_side
):
    boot = await _org(db_session)
    space = await _make_space(db_session, boot, "Animals")
    await _stored(db_session, boot, qdrant, space, BLUE, blobs, "lioness.png")
    await _stored(db_session, boot, qdrant, space, RED, blobs, "red.png")
    doc_id = (await _stored(db_session, boot, qdrant, space, RED, blobs, "x.png")).id
    text_side.spaces = [space.id]
    text_side.chunks = [
        RetrievedChunk(
            document_id=doc_id, space_id=space.id, document_name="notes.md", chunk_index=0,
            text="狮子是群居动物。", score=0.4, char_start=0, char_end=8,
        )
    ]

    resp = await _ask(http, db_session, redis_client, boot, {})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [p["document_name"] for p in body["passages"]] == ["notes.md"]
    # The stand-in embeds an unknown word as blue: only the blue picture is a close match.
    assert [i["name"] for i in body["images"]] == ["lioness.png"]
    image = body["images"][0]
    assert image["space_name"] == "Animals" and image["score"] >= 0.1
    assert image["preview_url"].startswith("https://api.example.test/api/v1/external/preview/")
    assert body["preview_expires_in"] > 0


async def test_kind_text_is_the_old_behaviour_and_never_touches_the_picture_model(
    http, db_session, redis_client, qdrant, blobs, text_side, embed_calls
):
    boot = await _org(db_session)
    space = await _make_space(db_session, boot, "Animals")
    await _stored(db_session, boot, qdrant, space, BLUE, blobs, "lioness.png")
    text_side.spaces = [space.id]

    resp = await _ask(http, db_session, redis_client, boot, {"kind": "text"})

    assert resp.status_code == 200
    assert resp.json()["images"] == [] and resp.json()["preview_expires_in"] == 0
    assert embed_calls == []


async def test_kind_image_searches_pictures_only_and_skips_the_text(
    http, db_session, redis_client, qdrant, blobs, text_side
):
    boot = await _org(db_session)
    space = await _make_space(db_session, boot, "Animals")
    await _stored(db_session, boot, qdrant, space, BLUE, blobs, "lioness.png")
    await _stored(db_session, boot, qdrant, space, RED, blobs, "red.png")

    resp = await _ask(http, db_session, redis_client, boot, {"kind": "image", "top_k": 5})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert text_side.calls == 0  # the documents were not searched
    assert body["passages"] == []
    # Pictures only: the closest ones even when weak, in order, like the dedicated endpoint.
    assert [i["name"] for i in body["images"]] == ["lioness.png", "red.png"]
    assert body["images"][0]["score"] > body["images"][1]["score"]
    audit = (
        await db_session.execute(
            select(AuditEvent.action).where(AuditEvent.action.like("knowledge.external_%search"))
        )
    ).scalars().all()
    assert audit == ["knowledge.external_image_search"]


async def test_a_weak_picture_is_not_offered_beside_text(
    http, db_session, redis_client, qdrant, blobs, text_side
):
    boot = await _org(db_session)
    space = await _make_space(db_session, boot, "Animals")
    await _stored(db_session, boot, qdrant, space, RED, blobs, "red.png")  # query points at blue
    text_side.spaces = [space.id]

    resp = await _ask(http, db_session, redis_client, boot, {})

    assert resp.json()["images"] == []


async def test_nothing_is_looked_up_when_there_is_no_picture_or_the_query_is_long(
    http, db_session, redis_client, qdrant, blobs, text_side, embed_calls
):
    boot = await _org(db_session)
    space = await _make_space(db_session, boot, "Docs")
    text_side.spaces = [space.id]

    none = await _ask(http, db_session, redis_client, boot, {})
    assert none.status_code == 200 and none.json()["images"] == []

    await _stored(db_session, boot, qdrant, space, BLUE, blobs, "a.png")
    long_query = await _ask(http, db_session, redis_client, boot, {"query": "报销流程怎么走" * 10})
    assert long_query.status_code == 200 and long_query.json()["images"] == []
    assert embed_calls == []  # the picture model was never asked


async def test_only_pictures_the_person_can_read_are_offered(
    http, db_session, redis_client, qdrant, blobs, text_side
):
    boot = await _org(db_session)
    mine = await _make_space(db_session, boot, "Mine")
    secret = await _make_space(db_session, boot, "Secret")
    member = await _member(db_session, boot)
    await _grant(db_session, boot, mine, member)
    await _stored(db_session, boot, qdrant, mine, BLUE, blobs, "visible.png")
    await _stored(db_session, boot, qdrant, secret, BLUE, blobs, "hidden.png")
    text_side.spaces = [mine.id]

    resp = await _ask(http, db_session, redis_client, boot, {}, user_id=member.id)

    assert [i["name"] for i in resp.json()["images"]] == ["visible.png"]
    assert "hidden.png" not in resp.text and "Secret" not in resp.text


async def test_a_named_space_narrows_the_pictures_too(
    http, db_session, redis_client, qdrant, blobs, text_side
):
    boot = await _org(db_session)
    a = await _make_space(db_session, boot, "Alpha")
    b = await _make_space(db_session, boot, "Beta")
    await _stored(db_session, boot, qdrant, a, BLUE, blobs, "in-alpha.png")
    await _stored(db_session, boot, qdrant, b, BLUE, blobs, "in-beta.png")
    text_side.spaces = [b.id]

    resp = await _ask(http, db_session, redis_client, boot, {"space": "Beta"})

    assert [i["name"] for i in resp.json()["images"]] == ["in-beta.png"]


async def test_a_picture_failure_never_breaks_the_text_search(
    http, db_session, redis_client, qdrant, blobs, text_side, monkeypatch
):
    boot = await _org(db_session)
    space = await _make_space(db_session, boot, "Animals")
    await _stored(db_session, boot, qdrant, space, BLUE, blobs, "lioness.png")
    text_side.spaces = [space.id]

    async def boom(**_k):
        raise RuntimeError("picture model could not load")

    monkeypatch.setattr(image_search, "embed_query", boom)

    resp = await _ask(http, db_session, redis_client, boot, {})

    assert resp.status_code == 200, resp.text
    assert resp.json()["images"] == [] and resp.json()["passages"] == []


async def test_the_picture_limit_degrades_a_text_search_but_blocks_a_picture_search(
    http, db_session, redis_client, qdrant, blobs, text_side, monkeypatch
):
    boot = await _org(db_session)
    space = await _make_space(db_session, boot, "Animals")
    await _stored(db_session, boot, qdrant, space, BLUE, blobs, "lioness.png")
    text_side.spaces = [space.id]
    monkeypatch.setattr(external_api, "_IMAGE_SEARCH_RATE_LIMIT", 1)
    tokens = await _read(db_session, redis_client, boot)
    headers = _auth(tokens)

    first = await http.post(SEARCH, headers=headers, json={"query": "动物母狮"})
    second = await http.post(SEARCH, headers=headers, json={"query": "动物母狮"})
    strict = await http.post(SEARCH, headers=headers, json={"query": "动物母狮", "kind": "image"})

    assert len(first.json()["images"]) == 1
    assert second.status_code == 200 and second.json()["images"] == []  # text still answers
    assert strict.status_code == 429 and strict.headers["retry-after"] == "60"


async def test_the_audit_names_the_pictures_by_id_only(
    http, db_session, redis_client, qdrant, blobs, text_side
):
    boot = await _org(db_session)
    space = await _make_space(db_session, boot, "Animals")
    pic = await _stored(db_session, boot, qdrant, space, BLUE, blobs, "secret-name.png")
    text_side.spaces = [space.id]

    await _ask(http, db_session, redis_client, boot, {})

    payload = (
        await db_session.execute(
            select(AuditEvent.payload).where(AuditEvent.action == "knowledge.external_search")
        )
    ).scalar_one()
    assert payload["related_picture_ids"] == [str(pic.id)]
    assert "secret-name.png" not in repr(payload) and "动物母狮" not in repr(payload)


async def test_bad_kinds_and_over_long_picture_descriptions_are_refused(
    http, db_session, redis_client, text_side
):
    boot = await _org(db_session)
    assert (await _ask(http, db_session, redis_client, boot, {"kind": "video"})).status_code == 422
    too_long = await _ask(http, db_session, redis_client, boot, {"kind": "image", "query": "x" * 501})
    assert too_long.status_code == 400
    assert too_long.json()["detail"]["code"] == "bad_query"
