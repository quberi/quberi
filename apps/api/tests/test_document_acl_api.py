# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""Document-level access over HTTP: the outside-tool API, the web API, chat, and the rules API.

Real: database, permission evaluator, picture vector store (in memory), OAuth grants,
conversation service. Stood in: the picture model and the file store.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from quberi_api.api import dependencies
from quberi_api.api.errors import register_exception_handlers
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.api.v1.conversations import router as conv_router
from quberi_api.api.v1.external import external_router
from quberi_api.api.v1.spaces import document_access, documents as documents_router
from quberi_api.api.v1.spaces import router as spaces_module
from quberi_api.core.config import get_settings
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.identity.models import Organization, User
from quberi_api.services import conversations as conversations_service
from quberi_api.services.documents import access_rules as rules
from tests.test_external_image_search import (  # noqa: F401
    _path,
    _read,
    _stored,
    signing_key,
)
from tests.test_external_save import _auth, _org, redis_client  # noqa: F401
from tests.test_image_indexing import _space as _make_space
from tests.test_image_search import RED, _grant, _member, fake_embedder  # noqa: F401
from tests.test_image_search_api import blobs, qdrant as chat_qdrant  # noqa: F401

SEARCH = "/api/v1/external/images/search"
SPACES = "/api/v1/external/spaces"
DOCS = "/api/v1/external/documents"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def qdrant():
    from qdrant_client import AsyncQdrantClient

    client = AsyncQdrantClient(":memory:")
    yield client
    await client.close()


@pytest.fixture
async def http(db_session, redis_client, qdrant, monkeypatch):
    """The outside-tool API."""
    from quberi_api.services import conversations as conv_service

    class Shared:
        def __getattr__(self, name):
            return getattr(qdrant, name)

        async def close(self):
            return None

    monkeypatch.setattr(conv_service, "create_qdrant_client", lambda _s: Shared())
    app = FastAPI()
    app.state.settings = get_settings()
    app.include_router(external_router, prefix="/api/v1")
    infra = SimpleNamespace(redis=redis_client, qdrant=qdrant)

    async def _session():
        yield db_session

    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[dependencies.get_infrastructure] = lambda: infra
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class Who:
    def __init__(self, user, organization):
        self.user, self.organization = user, organization


@pytest.fixture
async def web(db_session, qdrant, redis_client, monkeypatch):
    """The web app's API, with the caller switchable between requests."""
    from quberi_api.services import conversations as conv_service

    class Shared:
        def __getattr__(self, name):
            return getattr(qdrant, name)

        async def close(self):
            return None

    monkeypatch.setattr(conv_service, "create_qdrant_client", lambda _s: Shared())
    app = FastAPI()
    app.state.settings = get_settings()
    register_exception_handlers(app)
    app.include_router(conv_router.conversation_router, prefix="/api/v1")
    app.include_router(documents_router.router, prefix="/api/v1")
    app.include_router(document_access.router, prefix="/api/v1")
    app.include_router(spaces_module.router, prefix="/api/v1")

    async def _session():
        yield db_session

    who = Who(None, None)
    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[dependencies.get_infrastructure] = lambda: SimpleNamespace(
        redis=redis_client, qdrant=qdrant
    )
    app.dependency_overrides[require_caller] = lambda: CallerContext(
        user=who.user, organization=who.organization, is_platform_admin=False
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.who = who  # type: ignore[attr-defined]
        yield client


async def _as(web, db, boot, user_id):
    user = await db.get(User, user_id)
    org = await db.get(Organization, boot.organization_id)
    await db.commit()
    web.who.user, web.who.organization = user, org


async def _rule(db, boot, space, doc, effect, user_id, actor=None):
    await db.commit()
    view = await rules.add_rule(
        db, organization_id=boot.organization_id, actor_user_id=actor or boot.user_id,
        space_id=space.id, document_id=doc.id, effect=effect,
        subject_type="user", subject_id=user_id,
    )
    await db.commit()
    return view.rule


async def _team(db, redis, qdrant, blobs):
    """An organisation with a space of three pictures and an outsider with no space access."""
    boot = await _org(db)
    made = await _make_space(db, boot, "Parts")
    space = SimpleNamespace(id=made.id)  # plain values survive a rolled-back step

    async def pic(name):
        doc = await _stored(db, boot, qdrant, made, RED, blobs, name)
        return SimpleNamespace(id=doc.id)

    pics = SimpleNamespace(
        shared=await pic("shared.png"), other=await pic("other.png"), secret=await pic("secret.png")
    )
    outsider = await _member(db, boot)
    outsider = SimpleNamespace(id=outsider.id, display_name=outsider.display_name)
    return boot, space, pics, outsider


# ---------------------------------------------------------------------------
# Outside tools: pictures, previews, space and document lists
# ---------------------------------------------------------------------------


async def test_a_shared_picture_is_found_and_its_preview_opens_only_while_shared(
    http, db_session, redis_client, qdrant, blobs
):
    boot, space, pics, outsider = await _team(db_session, redis_client, qdrant, blobs)
    tokens = await _read(db_session, redis_client, boot, user_id=outsider.id)

    nothing = (await http.post(SEARCH, headers=_auth(tokens), json={"query": "red"})).json()
    assert nothing["images"] == []

    rule = await _rule(db_session, boot, space, pics.shared, "allow", outsider.id)
    found = (await http.post(SEARCH, headers=_auth(tokens), json={"query": "red"})).json()
    assert [i["name"] for i in found["images"]] == ["shared.png"]
    link = _path(found["images"][0]["preview_url"])
    assert (await http.get(link)).status_code == 200
    await db_session.commit()

    # Withdrawn: the link that was already handed out stops working at once.
    await db_session.commit()
    await rules.remove_rule(
        db_session, organization_id=boot.organization_id, actor_user_id=boot.user_id,
        space_id=space.id, document_id=pics.shared.id, rule_id=rule.id,
    )
    await db_session.commit()
    assert (await http.get(link)).status_code == 404
    await db_session.commit()
    again = (await http.post(SEARCH, headers=_auth(tokens), json={"query": "red"})).json()
    assert again["images"] == []


async def test_a_denied_picture_cannot_be_previewed_even_with_a_link_obtained_earlier(
    http, db_session, redis_client, qdrant, blobs
):
    boot, space, pics, outsider = await _team(db_session, redis_client, qdrant, blobs)
    await _grant(db_session, boot, space, outsider)
    tokens = await _read(db_session, redis_client, boot, user_id=outsider.id)
    found = (await http.post(SEARCH, headers=_auth(tokens), json={"query": "red"})).json()
    links = {i["name"]: _path(i["preview_url"]) for i in found["images"]}
    assert set(links) == {"shared.png", "other.png", "secret.png"}
    assert (await http.get(links["secret.png"])).status_code == 200
    await db_session.commit()

    await _rule(db_session, boot, space, pics.secret, "deny", outsider.id)

    assert (await http.get(links["secret.png"])).status_code == 404
    await db_session.commit()
    assert (await http.get(links["other.png"])).status_code == 200
    await db_session.commit()
    after = (await http.post(SEARCH, headers=_auth(tokens), json={"query": "red"})).json()
    assert {i["name"] for i in after["images"]} == {"shared.png", "other.png"}


async def test_the_space_list_marks_a_shared_only_space_and_counts_what_is_readable(
    http, db_session, redis_client, qdrant, blobs
):
    boot, space, pics, outsider = await _team(db_session, redis_client, qdrant, blobs)
    await _rule(db_session, boot, space, pics.shared, "allow", outsider.id)
    tokens = await _read(db_session, redis_client, boot, user_id=outsider.id)

    body = (await http.get(SPACES, headers=_auth(tokens))).json()
    parts = next(s for s in body["spaces"] if s["name"] == "Parts")
    assert parts["shared_only_documents"] == 1
    assert parts["document_count"] == 1
    assert parts["can_edit"] is False
    assert parts["kind"] == "shared"
    assert body["total"] == len(body["spaces"])

    docs = (await http.get(DOCS, headers=_auth(tokens), params={"space": "Parts"})).json()
    assert [d["name"] for d in docs["documents"]] == ["shared.png"]
    assert docs["total"] == 1  # the count and the list always agree


async def test_denied_documents_leave_the_list_and_the_counts_for_that_person_only(
    http, db_session, redis_client, qdrant, blobs
):
    boot, space, pics, outsider = await _team(db_session, redis_client, qdrant, blobs)
    await _grant(db_session, boot, space, outsider)
    await _rule(db_session, boot, space, pics.secret, "deny", outsider.id)
    theirs = await _read(db_session, redis_client, boot, user_id=outsider.id)
    owners = await _read(db_session, redis_client, boot)

    docs = (await http.get(DOCS, headers=_auth(theirs), params={"space": "Parts"})).json()
    assert {d["name"] for d in docs["documents"]} == {"shared.png", "other.png"}
    assert docs["total"] == 2
    parts = next(
        s for s in (await http.get(SPACES, headers=_auth(theirs))).json()["spaces"] if s["name"] == "Parts"
    )
    assert parts["document_count"] == 2 and parts["shared_only_documents"] == 0

    docs = (await http.get(DOCS, headers=_auth(owners), params={"space": "Parts"})).json()
    assert docs["total"] == 3
    parts = next(
        s for s in (await http.get(SPACES, headers=_auth(owners))).json()["spaces"] if s["name"] == "Parts"
    )
    assert parts["document_count"] == 3


async def test_a_text_search_may_name_a_shared_only_space(
    http, db_session, redis_client, qdrant, blobs, monkeypatch
):
    from quberi_api.api.v1 import external as external_api
    from quberi_api.services.doc_acl import NO_DOC_ACCESS

    boot, space, pics, outsider = await _team(db_session, redis_client, qdrant, blobs)
    await _rule(db_session, boot, space, pics.shared, "allow", outsider.id)
    seen = []

    async def fake_retrieve(session, *, qdrant, organization_id, caller_user_id, query, top_k, space_ids=None):
        seen.append(space_ids)
        return SimpleNamespace(chunks=[], space_ids=[], doc_access=NO_DOC_ACCESS)

    monkeypatch.setattr(external_api, "retrieve_for_user", fake_retrieve)
    tokens = await _read(db_session, redis_client, boot, user_id=outsider.id)

    ok = await http.post(SEARCH.replace("images/", ""), headers=_auth(tokens), json={"query": "q", "space": "Parts"})
    assert ok.status_code == 200, ok.text
    assert seen == [[space.id]]
    nope = await http.post(
        SEARCH.replace("images/", ""), headers=_auth(tokens), json={"query": "q", "space": "Nowhere"}
    )
    assert nope.status_code == 404


async def test_the_pending_list_never_names_a_hidden_document(
    http, db_session, redis_client, qdrant, blobs
):
    from quberi_api.api.v1.external import _pending_documents
    from quberi_api.domain.documents.models import Document
    from quberi_api.services.doc_acl import DocAccess
    from sqlalchemy import update

    boot, space, pics, outsider = await _team(db_session, redis_client, qdrant, blobs)
    for doc in (pics.shared, pics.secret):
        await db_session.execute(
            update(Document).where(Document.id == doc.id).values(indexing_state="indexing")
        )
    await db_session.commit()

    total, rows = await _pending_documents(
        db_session, organization_id=boot.organization_id, readable_space_ids=[space.id],
        doc_access=DocAccess(denied=frozenset({pics.secret.id})),
    )
    assert total == 1 and [r[0] for r in rows] == ["shared.png"]
    total, rows = await _pending_documents(
        db_session, organization_id=boot.organization_id, readable_space_ids=[],
        doc_access=DocAccess(granted={pics.shared.id: space.id}),
    )
    assert total == 1 and [r[0] for r in rows] == ["shared.png"]


# ---------------------------------------------------------------------------
# The web app: lists, details, chat, scope
# ---------------------------------------------------------------------------


async def test_the_web_space_list_and_documents_follow_the_same_rules(
    web, db_session, redis_client, qdrant, blobs
):
    boot, space, pics, outsider = await _team(db_session, redis_client, qdrant, blobs)
    await _rule(db_session, boot, space, pics.shared, "allow", outsider.id)
    await _as(web, db_session, boot, outsider.id)

    listing = (await web.get("/api/v1/spaces")).json()
    mine = next(s for s in listing["items"] if s["id"] == str(space.id))
    assert mine["shared_only_documents"] == 1
    assert (await web.get(f"/api/v1/spaces/{space.id}")).json()["shared_only_documents"] == 1

    page = (await web.get(f"/api/v1/spaces/{space.id}/documents")).json()
    assert [d["name"] for d in page["items"]] == ["shared.png"] and page["total"] == 1
    stats = (await web.get(f"/api/v1/spaces/{space.id}/documents/stats")).json()
    assert stats["total"] == 1
    assert (await web.get(f"/api/v1/spaces/{space.id}/documents/{pics.shared.id}")).status_code == 200
    assert (await web.get(f"/api/v1/spaces/{space.id}/documents/{pics.other.id}")).status_code == 404
    for path in ("download", "thumbnail", "text"):
        shared = await web.get(f"/api/v1/spaces/{space.id}/documents/{pics.shared.id}/{path}")
        other = await web.get(f"/api/v1/spaces/{space.id}/documents/{pics.other.id}/{path}")
        assert other.status_code == 404, path
        assert shared.status_code != 404, path


async def test_a_denied_document_is_a_404_on_every_web_door(
    web, db_session, redis_client, qdrant, blobs
):
    boot, space, pics, outsider = await _team(db_session, redis_client, qdrant, blobs)
    await _grant(db_session, boot, space, outsider)
    await _rule(db_session, boot, space, pics.secret, "deny", outsider.id)
    await _as(web, db_session, boot, outsider.id)

    base = f"/api/v1/spaces/{space.id}/documents"
    names = {d["name"] for d in (await web.get(base)).json()["items"]}
    assert names == {"shared.png", "other.png"}
    for suffix in ("", "/download", "/thumbnail", "/text"):
        assert (await web.get(f"{base}/{pics.secret.id}{suffix}")).status_code == 404, suffix
    assert (await web.post(f"{base}/{pics.secret.id}/reindex")).status_code == 404
    assert (await web.delete(f"{base}/{pics.secret.id}")).status_code == 404


async def test_chat_lists_and_finds_only_the_pictures_the_person_may_read(
    web, db_session, redis_client, qdrant, blobs
):
    boot, space, pics, outsider = await _team(db_session, redis_client, qdrant, blobs)
    await _rule(db_session, boot, space, pics.shared, "allow", outsider.id)
    personal = (await _personal(db_session, outsider.id))
    await _as(web, db_session, boot, outsider.id)
    conv = (
        await conversations_service.create_conversation(
            db_session, space_id=personal, organization_id=boot.organization_id,
            user_id=outsider.id, title=None,
        )
    ).conversation
    await db_session.commit()

    listing = await _ask(web, conv, "库里有哪些图片")
    assert [m["document_name"] for m in listing["image_matches"]] == ["shared.png"]
    found = await _ask(web, conv, "有没有红色的图片")
    assert [m["document_name"] for m in found["image_matches"]] == ["shared.png"]

    # Now they can read the whole space, minus one picture hidden from them.
    await _grant(db_session, boot, space, outsider)
    await _rule(db_session, boot, space, pics.secret, "deny", outsider.id)
    after = await _ask(web, conv, "库里有哪些图片")
    assert {m["document_name"] for m in after["image_matches"]} == {"shared.png", "other.png"}


async def test_a_shared_only_space_can_be_picked_as_a_chat_source(
    db_session, redis_client, qdrant, blobs
):
    boot, space, pics, outsider = await _team(db_session, redis_client, qdrant, blobs)
    personal = await _personal(db_session, outsider.id)
    conv = (
        await conversations_service.create_conversation(
            db_session, space_id=personal, organization_id=boot.organization_id,
            user_id=outsider.id, title=None,
        )
    ).conversation.id
    await db_session.commit()
    with pytest.raises(Exception):  # nothing shared yet: the space does not exist for them
        await conversations_service.add_scope_source(
            db_session, conversation_id=conv, space_id=space.id,
            organization_id=boot.organization_id, caller_user_id=outsider.id,
        )
    await db_session.rollback()

    await _rule(db_session, boot, space, pics.shared, "allow", outsider.id)
    view = await conversations_service.add_scope_source(
        db_session, conversation_id=conv, space_id=space.id,
        organization_id=boot.organization_id, caller_user_id=outsider.id,
    )
    assert view.space_id == space.id


async def _personal(db, user_id):
    from quberi_api.domain.spaces.models import Space

    sid = (
        await db.execute(
            select(Space.id).where(Space.owner_id == user_id, Space.preset == "personal")
        )
    ).scalar_one()
    await db.commit()
    return sid


async def _ask(web, conv, text):
    resp = await web.post(
        f"/api/v1/conversations/{conv.id}/messages/stream", json={"text": text}
    )
    assert resp.status_code == 200, resp.text
    for block in resp.text.split("\n\n"):
        if block.startswith("event: assistant_message"):
            data = next(line[6:] for line in block.splitlines() if line.startswith("data: "))
            return json.loads(data)["content"]
    raise AssertionError(f"no assistant message in {resp.text!r}")


# ---------------------------------------------------------------------------
# The rules API
# ---------------------------------------------------------------------------


async def test_the_rules_api_adds_lists_and_removes_with_the_right_status_codes(
    web, db_session, redis_client, qdrant, blobs
):
    boot, space, pics, outsider = await _team(db_session, redis_client, qdrant, blobs)
    viewer = await _member(db_session, boot)
    await _grant(db_session, boot, space, viewer)
    base = f"/api/v1/spaces/{space.id}/documents/{pics.secret.id}/access-rules"
    body = {"effect": "deny", "subject_type": "user", "subject_id": str(viewer.id)}

    await _as(web, db_session, boot, viewer.id)  # a plain viewer: not a manager
    assert (await web.get(base)).status_code == 403
    assert (await web.post(base, json=body)).status_code == 403

    await _as(web, db_session, boot, boot.user_id)
    created = await web.post(base, json=body)
    assert created.status_code == 201, created.text
    rule = created.json()
    assert rule["effect"] == "deny" and rule["subject_display_name"] == viewer.display_name
    assert (await web.post(base, json=body)).status_code == 409
    assert (
        await web.post(base, json={**body, "subject_id": "00000000-0000-0000-0000-000000000000"})
    ).status_code == 404
    assert (await web.post(base, json={**body, "effect": "maybe"})).status_code == 422
    assert (await web.post(base, json={**body, "extra": 1})).status_code == 422
    listed = (await web.get(base)).json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == rule["id"]
    # "Everyone in the organisation" needs no id: the server uses the caller's own.
    whole = await web.post(base, json={"effect": "allow", "subject_type": "organization"})
    assert whole.status_code == 201, whole.text
    assert whole.json()["subject_id"] == str(boot.organization_id)
    assert (await web.post(base, json={"effect": "allow", "subject_type": "user"})).status_code == 422
    assert (await web.delete(f"{base}/{whole.json()['id']}")).status_code == 204

    await _as(web, db_session, boot, viewer.id)  # takes effect on the very next request
    docs = (await web.get(f"/api/v1/spaces/{space.id}/documents")).json()
    assert "secret.png" not in {d["name"] for d in docs["items"]}

    await _as(web, db_session, boot, boot.user_id)
    assert (await web.delete(f"{base}/{rule['id']}")).status_code == 204
    assert (await web.delete(f"{base}/{rule['id']}")).status_code == 404
    await _as(web, db_session, boot, viewer.id)
    docs = (await web.get(f"/api/v1/spaces/{space.id}/documents")).json()
    assert "secret.png" in {d["name"] for d in docs["items"]}

    actions = (
        await db_session.execute(
            select(AuditEvent.action).where(AuditEvent.action.like("document.access_rule_%"))
        )
    ).scalars().all()
    # deny added + removed, whole-organisation allow added + removed
    assert sorted(actions) == ["document.access_rule_added"] * 2 + ["document.access_rule_removed"] * 2


async def test_the_manager_document_listing_gives_names_and_counts_only(
    web, db_session, redis_client, qdrant, blobs
):
    boot, space, pics, outsider = await _team(db_session, redis_client, qdrant, blobs)
    viewer = await _member(db_session, boot)
    await _grant(db_session, boot, space, viewer)
    await _rule(db_session, boot, space, pics.secret, "deny", viewer.id)
    url = f"/api/v1/spaces/{space.id}/access-rules/documents"

    await _as(web, db_session, boot, boot.user_id)
    body = (await web.get(url)).json()
    assert body["total"] == 3
    assert set(body["items"][0]) == {
        "id", "name", "mime_type", "size_bytes", "updated_at", "allow_rules", "deny_rules"
    }
    assert {i["name"]: i["deny_rules"] for i in body["items"]}["secret.png"] == 1
    assert [i["name"] for i in (await web.get(url, params={"q": "sec"})).json()["items"]] == ["secret.png"]

    await _as(web, db_session, boot, viewer.id)
    assert (await web.get(url)).status_code == 403
    await _as(web, db_session, boot, outsider.id)
    assert (await web.get(url)).status_code == 404
