# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""Text search can be limited to one named space, never widened past what you can read."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from quberi_api.api.v1 import external as external_api
from quberi_api.services import spaces as spaces_service
from quberi_api.services.rag import retrieve
from tests.test_external_save import (  # noqa: F401
    _auth,
    _no_infra,
    _org,
    _tokens,
    http,
    redis_client,
)

SEARCH = "/api/v1/external/search"


async def _space(db, boot, name):
    await db.commit()
    view = await spaces_service.create_collaborative_space(
        db,
        organization_id=boot.organization_id,
        creator_user_id=boot.user_id,
        name=name,
        slug=f"s-{uuid.uuid4().hex[:8]}",
    )
    await db.commit()
    return view.space


def _spy(monkeypatch):
    seen: list = []

    async def fake_retrieve(
        session, *, qdrant, organization_id, caller_user_id, query, top_k, space_ids=None
    ):
        seen.append(space_ids)
        return SimpleNamespace(chunks=[], space_ids=list(space_ids or []))

    monkeypatch.setattr(external_api, "retrieve_for_user", fake_retrieve)
    return seen


async def _post(http, db, redis, boot, body):
    tokens = await _tokens(db, redis, boot, scope="knowledge:read")
    await db.commit()
    return await http.post(SEARCH, headers=_auth(tokens), json={"query": "q", **body})


async def test_a_named_space_limits_the_search_to_it(
    http, db_session, redis_client, _no_infra, monkeypatch
):
    boot = await _org(db_session)
    await _space(db_session, boot, "Alpha")
    beta = await _space(db_session, boot, "Beta")
    seen = _spy(monkeypatch)

    resp = await _post(http, db_session, redis_client, boot, {"space": "Beta"})

    assert resp.status_code == 200, resp.text
    assert seen == [[beta.id]]
    assert resp.json()["searched_spaces"] == 1


async def test_without_a_space_every_readable_space_is_searched(
    http, db_session, redis_client, _no_infra, monkeypatch
):
    boot = await _org(db_session)
    seen = _spy(monkeypatch)

    for body in ({}, {"space": None}, {"space": "   "}):
        resp = await _post(http, db_session, redis_client, boot, body)
        assert resp.status_code == 200, resp.text

    assert seen == [None, None, None]


async def test_an_unknown_space_is_a_404_and_searches_nothing(
    http, db_session, redis_client, _no_infra, monkeypatch
):
    boot = await _org(db_session)
    seen = _spy(monkeypatch)

    resp = await _post(http, db_session, redis_client, boot, {"space": "Nope"})

    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "space_not_found"
    assert seen == []


async def test_naming_a_space_cannot_reach_beyond_what_the_person_may_read(
    db_session, org_and_owner, monkeypatch
):
    """The narrowing is an intersection with live permissions, not a replacement."""

    mine = await _space(db_session, org_and_owner, "Mine")
    not_mine = uuid.uuid4()  # a space the caller has no right to (or that does not exist)
    captured: dict = {}

    async def fake_search(session, *, qdrant, organization_id, space_ids, query, top_k):
        captured["space_ids"] = list(space_ids)
        return [], "m", "c"

    monkeypatch.setattr(retrieve, "_search_scoped_spaces", fake_search)

    result = await retrieve.retrieve_for_user(
        db_session, qdrant=None, organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id, query="q", space_ids=[mine.id, not_mine],
    )
    assert captured["space_ids"] == [mine.id]
    assert result.space_ids == [mine.id]

    only_foreign = await retrieve.retrieve_for_user(
        db_session, qdrant=None, organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id, query="q", space_ids=[not_mine],
    )
    assert only_foreign.space_ids == []
    assert captured["space_ids"] == []
