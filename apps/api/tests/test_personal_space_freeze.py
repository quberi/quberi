# ruff: noqa: F811, E501  (fixtures are imported by name; some assertion lines run long)
"""A frozen personal space is closed to everyone, and closes the same way at every door.

Real database, real permission evaluator, real vector filters (qdrant-client's in-memory
engine). Only the embedding models and the file store are stand-ins. The person who leaves
owns a real ``preset='personal'`` space; the organisation is a team one, and the tests that
walk the doors also ask for ``strict_rules`` so the super-administrator rules are on.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from qdrant_client import AsyncQdrantClient
from sqlalchemy import func, select, update

from quberi_api.api import dependencies
from quberi_api.api.errors import register_exception_handlers
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.api.v1.conversations import router as conv_router
from quberi_api.api.v1.external import external_router
from quberi_api.api.v1.spaces import access as access_router
from quberi_api.api.v1.spaces import documents as documents_router
from quberi_api.api.v1.spaces import router as spaces_module
from quberi_api.core.config import get_settings
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document
from quberi_api.domain.identity.models import Organization, User
from quberi_api.domain.spaces.models import Space
from quberi_api.services import authz
from quberi_api.services import conversations as conversations_service
from quberi_api.services import documents as documents_service
from quberi_api.services import space_bindings as bindings_service
from quberi_api.services import spaces as spaces_service
from quberi_api.services.documents import core as documents_core
from quberi_api.services.documents.errors import DocumentPermissionError
from quberi_api.services.errors import NotFoundError
from quberi_api.services.rag import image_search, retrieve
from quberi_api.services.space_freeze import freeze_personal_spaces, unfreeze_personal_spaces
from tests.test_document_acl import TEXT_DIM, TEXT_MODEL, _can, _rule, _text_doc
from tests.test_external_image_search import _path, _read, _stored, signing_key  # noqa: F401
from tests.test_external_save import _auth, _org, redis_client  # noqa: F401
from tests.test_image_indexing import _space as _make_space
from tests.test_image_search import RED, FakeEmbedder, _grant
from tests.test_image_search import _member as _invite
from tests.test_image_search_api import blobs  # noqa: F401

SEARCH = "/api/v1/external/search"
IMAGES = "/api/v1/external/images/search"
SPACES = "/api/v1/external/spaces"
DOCS = "/api/v1/external/documents"


# ---------------------------------------------------------------------------
# Stand-ins and fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def qdrant(monkeypatch):
    client = AsyncQdrantClient(":memory:")

    class Shared:
        def __getattr__(self, name):
            return getattr(client, name)

        async def close(self):
            return None

    monkeypatch.setattr(conversations_service, "create_qdrant_client", lambda _s: Shared())
    yield client
    await client.close()


@pytest.fixture(autouse=True)
def fake_models(monkeypatch):
    monkeypatch.setattr(image_search, "get_image_embedder", lambda *_a, **_k: FakeEmbedder())

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
async def http(db_session, redis_client, qdrant):
    """The outside-tool API."""
    app = FastAPI()
    app.state.settings = get_settings()
    app.include_router(external_router, prefix="/api/v1")
    infra = SimpleNamespace(redis=redis_client, qdrant=qdrant)

    async def _session():
        yield db_session

    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[dependencies.get_infrastructure] = lambda: infra
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


class Who:
    def __init__(self):
        self.user_id = self.organization_id = None


@pytest.fixture
async def web(db_session, qdrant, redis_client):
    """The web app's API, with the caller switchable between requests."""
    app = FastAPI()
    app.state.settings = get_settings()
    register_exception_handlers(app)
    app.include_router(conv_router.conversation_router, prefix="/api/v1")
    app.include_router(conv_router.space_scoped_router, prefix="/api/v1")
    app.include_router(documents_router.router, prefix="/api/v1")
    app.include_router(spaces_module.router, prefix="/api/v1")
    app.include_router(access_router.router, prefix="/api/v1")

    async def _session():
        yield db_session

    who = Who()
    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[dependencies.get_infrastructure] = lambda: SimpleNamespace(
        redis=redis_client, qdrant=qdrant
    )
    async def _caller():
        # Fresh rows on every request: a failed request rolls back and expires the old ones.
        user = await db_session.get(User, who.user_id)
        org = await db_session.get(Organization, who.organization_id)
        await db_session.refresh(user)
        await db_session.refresh(org)
        await db_session.commit()  # services open their own transaction
        return CallerContext(user=user, organization=org, is_platform_admin=False)

    app.dependency_overrides[require_caller] = _caller
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        c.who = who  # type: ignore[attr-defined]
        yield c


async def _as(web, db, boot, user_id):
    await db.commit()
    web.who.user_id, web.who.organization_id = user_id, boot.organization_id


async def _person(db, boot, role="member"):
    user = await _invite(db, boot, role)
    return SimpleNamespace(id=user.id, display_name=user.display_name)


async def _personal_of(db, user_id):
    sid = (
        await db.execute(select(Space.id).where(Space.owner_id == user_id, Space.preset == "personal"))
    ).scalar_one()
    await db.commit()
    return sid


async def _freeze(db, boot, user_id, *, suspend=True):
    """What deactivating a member does to the person's personal space (and status)."""
    await db.commit()
    async with db.begin():
        if suspend:
            await db.execute(update(User).where(User.id == user_id).values(status="suspended"))
        await freeze_personal_spaces(
            db,
            organization_id=boot.organization_id,
            owner_user_id=user_id,
            actor_user_id=boot.user_id,
        )


async def _unfreeze(db, boot, user_id, *, restore=True):
    await db.commit()
    async with db.begin():
        if restore:
            await db.execute(update(User).where(User.id == user_id).values(status="active"))
        await unfreeze_personal_spaces(
            db,
            organization_id=boot.organization_id,
            owner_user_id=user_id,
            actor_user_id=boot.user_id,
        )


async def _world(db, qdrant, blobs):
    """An organisation, a person who will leave (private notes and a picture), and others."""
    boot = await _org(db)
    admin = await _person(db, boot, "admin")
    colleague = await _person(db, boot)
    leaver = await _person(db, boot)
    private_id = await _personal_of(db, leaver.id)
    private = SimpleNamespace(id=private_id)
    # Members' personal spaces all carry the same default name; make this one tell apart.
    await db.execute(update(Space).where(Space.id == private_id).values(name="Leaver private"))
    await db.commit()
    private_note = await _text_doc(db, boot, qdrant, private, "private-notes.pdf")
    private_pic = SimpleNamespace(
        id=(await _stored(db, boot, qdrant, private, RED, blobs, "private.png")).id
    )
    shared_made = await _make_space(db, boot, "Handbook")
    shared = SimpleNamespace(id=shared_made.id)
    await _grant(db, boot, shared, SimpleNamespace(id=colleague.id), "viewer")
    handbook = await _text_doc(db, boot, qdrant, shared, "handbook.pdf")
    await db.execute(
        update(Document).where(Document.id == handbook.id).values(uploaded_by_user_id=leaver.id)
    )
    await db.commit()
    return SimpleNamespace(
        boot=boot,
        admin=admin,
        colleague=colleague,
        leaver=leaver,
        private=private,
        private_note=private_note,
        private_pic=private_pic,
        shared=shared,
        handbook=handbook,
    )


def _strict(monkeypatch):
    """Turn the super-administrator rules on once the fixtures are built (they create spaces)."""
    from quberi_api.services import access_guard
    from tests.conftest import _REAL_IS_RESTRICTED_ORG

    monkeypatch.setattr(access_guard, "is_restricted_org", _REAL_IS_RESTRICTED_ORG)


# The person whose space is frozen but who is not suspended: only the owner shortcut is left to close.
STILL_ACTIVE = "the person, still active"
EVERY_CALLER = ["organization owner", "admin", "colleague", "the person", STILL_ACTIVE]


def _callers(w):
    return {
        "organization owner": w.boot.user_id,
        "admin": w.admin.id,
        "colleague": w.colleague.id,
        "the person": w.leaver.id,
        STILL_ACTIVE: w.leaver.id,
    }


# ---------------------------------------------------------------------------
# The permission evaluator
# ---------------------------------------------------------------------------


async def test_a_frozen_space_leaves_every_accessible_set_and_the_owner_shortcuts(
    db_session, qdrant, blobs
):
    w = await _world(db_session, qdrant, blobs)
    org = w.boot.organization_id
    before = await authz.list_accessible_space_ids(db_session, user_id=w.leaver.id, organization_id=org)
    assert w.private.id in before
    assert await documents_core._caller_space_role(
        db_session, user_id=w.leaver.id, organization_id=org, space_id=w.private.id
    ) == "owner"

    await _freeze(db_session, w.boot, w.leaver.id, suspend=False)  # still active: shortcut only

    after = await authz.list_accessible_space_ids(db_session, user_id=w.leaver.id, organization_id=org)
    assert w.private.id not in after
    assert await documents_core._caller_space_role(
        db_session, user_id=w.leaver.id, organization_id=org, space_id=w.private.id
    ) is None
    assert not await conversations_service._caller_has_space_view(
        db_session, user_id=w.leaver.id, organization_id=org, space_id=w.private.id
    )
    # Nobody else could read it in the first place, and still cannot.
    for who in (w.boot.user_id, w.admin.id, w.colleague.id):
        assert w.private.id not in await authz.list_accessible_space_ids(
            db_session, user_id=who, organization_id=org
        )
        assert await documents_core._caller_space_role(
            db_session, user_id=who, organization_id=org, space_id=w.private.id
        ) is None


async def test_a_suspended_user_has_no_access_anywhere_but_the_shared_space_keeps_its_content(
    db_session, qdrant, blobs
):
    w = await _world(db_session, qdrant, blobs)
    org = w.boot.organization_id
    await _grant(db_session, w.boot, w.shared, SimpleNamespace(id=w.leaver.id), "editor")
    assert w.shared.id in await authz.list_accessible_space_ids(
        db_session, user_id=w.leaver.id, organization_id=org
    )
    await _freeze(db_session, w.boot, w.leaver.id)

    assert await authz.list_accessible_space_ids(db_session, user_id=w.leaver.id, organization_id=org) == set()
    assert await authz.resolve_effective_role_bindings(
        db_session,
        user_id=w.leaver.id,
        organization_id=org,
        resource_type="space",
        resource_id=w.shared.id,
    ) == []
    members = await authz.list_effective_space_members(
        db_session, organization_id=org, space_id=w.shared.id
    )
    assert w.leaver.id not in {m.user_id for m in members}
    assert w.colleague.id in {m.user_id for m in members}
    # The shared space and what the person uploaded to it are untouched.
    status = (await db_session.execute(select(Space.status).where(Space.id == w.shared.id))).scalar_one()
    assert status == "active"
    assert (
        await db_session.execute(select(func.count()).select_from(Document).where(Document.id == w.handbook.id))
    ).scalar_one() == 1


async def test_a_space_for_the_whole_organisation_leaves_out_a_suspended_member(
    db_session, qdrant, blobs
):
    w = await _world(db_session, qdrant, blobs)
    org = w.boot.organization_id
    await db_session.commit()
    await bindings_service.add_binding(
        db_session,
        space_id=w.shared.id,
        organization_id=org,
        actor_user_id=w.boot.user_id,
        subject_type="organization",
        subject_id=org,
        role_code="viewer",
    )
    await db_session.commit()
    await _freeze(db_session, w.boot, w.leaver.id)
    members = await authz.list_effective_space_members(
        db_session, organization_id=org, space_id=w.shared.id
    )
    assert w.leaver.id not in {m.user_id for m in members}
    assert {w.colleague.id, w.admin.id} <= {m.user_id for m in members}


async def test_the_organisation_creators_own_personal_space_has_no_binding_and_is_still_closed(
    db_session, qdrant, blobs
):
    """The shortcut by owner id is the only thing that opens it, so freezing must close it."""
    w = await _world(db_session, qdrant, blobs)
    org = w.boot.organization_id
    mine = await _personal_of(db_session, w.boot.user_id)
    assert mine in await authz.list_accessible_space_ids(
        db_session, user_id=w.boot.user_id, organization_id=org
    )
    await _freeze(db_session, w.boot, w.boot.user_id, suspend=False)
    assert mine not in await authz.list_accessible_space_ids(
        db_session, user_id=w.boot.user_id, organization_id=org
    )
    assert await documents_core._caller_space_role(
        db_session, user_id=w.boot.user_id, organization_id=org, space_id=mine
    ) is None


async def test_an_archived_space_is_still_readable_and_frozen_is_not(db_session, qdrant, blobs):
    w = await _world(db_session, qdrant, blobs)
    org = w.boot.organization_id
    await db_session.commit()
    await spaces_service.archive_space(
        db_session, space_id=w.shared.id, organization_id=org, actor_user_id=w.boot.user_id
    )
    assert w.shared.id in await authz.list_accessible_space_ids(
        db_session, user_id=w.colleague.id, organization_id=org
    )
    await db_session.commit()
    await db_session.execute(update(Space).where(Space.id == w.shared.id).values(status="frozen"))
    await db_session.commit()
    assert w.shared.id not in await authz.list_accessible_space_ids(
        db_session, user_id=w.colleague.id, organization_id=org
    )


# ---------------------------------------------------------------------------
# Freeze and unfreeze themselves
# ---------------------------------------------------------------------------


async def _audit_count(db, action, space_id):
    await db.commit()
    return (
        await db.execute(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.action == action, AuditEvent.space_id == space_id
            )
        )
    ).scalar_one()


async def test_freezing_twice_audits_once_and_keeps_the_data(db_session, qdrant, blobs):
    w = await _world(db_session, qdrant, blobs)
    await _freeze(db_session, w.boot, w.leaver.id)
    await _freeze(db_session, w.boot, w.leaver.id)
    assert await _audit_count(db_session, "space.frozen", w.private.id) == 1
    space = (await db_session.execute(select(Space).where(Space.id == w.private.id))).scalar_one()
    assert space.status == "frozen"
    assert space.attributes["frozen"]["from_status"] == "active"
    assert space.attributes["frozen"]["reason"] == "member_deactivated"
    docs = (
        await db_session.execute(select(func.count()).select_from(Document).where(Document.space_id == w.private.id))
    ).scalar_one()
    assert docs == 2  # nothing deleted
    payload = (
        await db_session.execute(
            select(AuditEvent.payload).where(AuditEvent.action == "space.frozen", AuditEvent.space_id == w.private.id)
        )
    ).scalar_one()
    assert payload["from_status"] == "active" and payload["member_id"] == str(w.leaver.id)


async def test_only_personal_spaces_of_that_member_are_frozen(db_session, qdrant, blobs):
    w = await _world(db_session, qdrant, blobs)
    await _freeze(db_session, w.boot, w.leaver.id)
    rows = dict(
        (await db_session.execute(select(Space.id, Space.status).where(Space.organization_id == w.boot.organization_id))).all()
    )
    assert rows[w.private.id] == "frozen"
    assert rows[w.shared.id] == "active"
    colleague_space = await _personal_of(db_session, w.colleague.id)
    assert rows[colleague_space] == "active"


async def test_unfreezing_restores_the_status_the_space_had(db_session, qdrant, blobs):
    w = await _world(db_session, qdrant, blobs)
    await db_session.commit()
    await db_session.execute(update(Space).where(Space.id == w.private.id).values(status="archived"))
    await db_session.commit()
    await _freeze(db_session, w.boot, w.leaver.id)
    await _unfreeze(db_session, w.boot, w.leaver.id)
    space = (await db_session.execute(select(Space).where(Space.id == w.private.id))).scalar_one()
    assert space.status == "archived"
    assert "frozen" not in space.attributes
    assert await _audit_count(db_session, "space.unfrozen", w.private.id) == 1
    await _unfreeze(db_session, w.boot, w.leaver.id, restore=False)  # nothing left to do
    assert await _audit_count(db_session, "space.unfrozen", w.private.id) == 1


async def test_a_frozen_space_that_was_not_frozen_by_a_deactivation_is_left_alone(
    db_session, qdrant, blobs
):
    w = await _world(db_session, qdrant, blobs)
    await db_session.commit()
    await db_session.execute(update(Space).where(Space.id == w.private.id).values(status="frozen"))
    await db_session.commit()
    await _unfreeze(db_session, w.boot, w.leaver.id)
    status = (await db_session.execute(select(Space.status).where(Space.id == w.private.id))).scalar_one()
    assert status == "frozen"


# ---------------------------------------------------------------------------
# The web doors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("who", EVERY_CALLER)
async def test_every_web_door_answers_404_for_every_kind_of_caller(
    web, db_session, qdrant, blobs, monkeypatch, who
):
    w = await _world(db_session, qdrant, blobs)
    _strict(monkeypatch)
    conv = (
        await conversations_service.create_conversation(
            db_session,
            space_id=w.private.id,
            organization_id=w.boot.organization_id,
            user_id=w.leaver.id,
            title="mine",
        )
    ).conversation.id
    await db_session.commit()
    await _freeze(db_session, w.boot, w.leaver.id, suspend=who != STILL_ACTIVE)
    await _as(web, db_session, w.boot, _callers(w)[who])

    sid, doc = w.private.id, w.private_note.id
    listing = (await web.get("/api/v1/spaces")).json()
    assert str(sid) not in {s["id"] for s in listing["items"]}
    assert (await web.get(f"/api/v1/spaces/{sid}")).status_code == 404
    base = f"/api/v1/spaces/{sid}/documents"
    assert (await web.get(base)).status_code == 404
    assert (await web.get(f"{base}/stats")).status_code == 404
    for suffix in ("", "/download", "/thumbnail", "/text"):
        assert (await web.get(f"{base}/{doc}{suffix}")).status_code == 404, suffix
    assert (await web.get(f"/api/v1/spaces/{sid}/conversations")).status_code == 404
    assert (await web.get(f"/api/v1/conversations/{conv}/messages")).status_code == 404
    sent = await web.post(f"/api/v1/conversations/{conv}/messages/stream", json={"text": "hello"})
    assert "event: error" in sent.text and "event: assistant_message" not in sent.text
    assert "event: user_message" not in sent.text


@pytest.mark.parametrize("who", EVERY_CALLER)
async def test_every_web_write_is_refused_on_a_frozen_space(
    web, db_session, qdrant, blobs, monkeypatch, who
):
    w = await _world(db_session, qdrant, blobs)
    _strict(monkeypatch)
    await _freeze(db_session, w.boot, w.leaver.id, suspend=who != STILL_ACTIVE)
    await _as(web, db_session, w.boot, _callers(w)[who])
    sid = w.private.id

    def refused(resp):
        assert 400 <= resp.status_code < 500, resp.text

    refused(await web.post(f"/api/v1/spaces/{sid}/conversations", json={"title": "x"}))
    refused(await web.post(f"/api/v1/spaces/{sid}/notes", json={"title": "x", "text": "y"}))
    refused(
        await web.post(
            f"/api/v1/spaces/{sid}/documents",
            files={"file": ("a.txt", b"hello", "text/plain")},
        )
    )
    refused(await web.delete(f"/api/v1/spaces/{sid}/documents/{w.private_note.id}"))
    refused(await web.post(f"/api/v1/spaces/{sid}:archive"))
    refused(
        await web.post(
            f"/api/v1/spaces/{sid}/access-bindings",
            json={"subject_type": "user", "subject_id": str(w.colleague.id), "role_code": "viewer"},
        )
    )
    refused(await web.get(f"/api/v1/spaces/{sid}/access-bindings"))
    # Nothing was written.
    assert (
        await db_session.execute(select(func.count()).select_from(Document).where(Document.space_id == sid))
    ).scalar_one() == 2
    assert (await db_session.execute(select(Space.status).where(Space.id == sid))).scalar_one() == "frozen"


async def test_the_services_refuse_to_write_to_a_frozen_space(db_session, qdrant, blobs):
    w = await _world(db_session, qdrant, blobs)
    org = w.boot.organization_id
    await _freeze(db_session, w.boot, w.leaver.id, suspend=False)
    with pytest.raises((NotFoundError, DocumentPermissionError)):
        await documents_service.save_note(
            db_session,
            organization_id=org,
            space_id=w.private.id,
            author_user_id=w.leaver.id,
            title="t",
            text="x",
        )
    await db_session.rollback()
    with pytest.raises(NotFoundError):
        await conversations_service.create_conversation(
            db_session,
            space_id=w.private.id,
            organization_id=org,
            user_id=w.leaver.id,
            title=None,
        )
    await db_session.rollback()
    with pytest.raises(NotFoundError):
        await bindings_service.add_binding(
            db_session,
            space_id=w.private.id,
            organization_id=org,
            actor_user_id=w.leaver.id,
            subject_type="user",
            subject_id=w.colleague.id,
            role_code="viewer",
        )
    await db_session.rollback()
    with pytest.raises(NotFoundError):
        await bindings_service.remove_binding(
            db_session,
            binding_id=uuid.uuid4(),
            space_id=w.private.id,
            organization_id=org,
            actor_user_id=w.leaver.id,
        )
    await db_session.rollback()
    with pytest.raises(NotFoundError):
        await conversations_service.list_conversations_for_space(
            db_session, space_id=w.private.id, organization_id=org, user_id=w.leaver.id
        )
    await db_session.rollback()


async def test_a_conversation_cannot_take_a_frozen_space_as_a_source(db_session, qdrant, blobs):
    w = await _world(db_session, qdrant, blobs)
    org = w.boot.organization_id
    conv = (
        await conversations_service.create_conversation(
            db_session, space_id=await _personal_of(db_session, w.colleague.id), organization_id=org,
            user_id=w.colleague.id, title=None,
        )
    ).conversation.id
    await db_session.commit()
    await _freeze(db_session, w.boot, w.leaver.id)
    with pytest.raises(NotFoundError):
        await conversations_service.add_scope_source(
            db_session, conversation_id=conv, space_id=w.private.id,
            organization_id=org, caller_user_id=w.colleague.id,
        )
    await db_session.rollback()


async def _ask(web, conv, text):
    resp = await web.post(f"/api/v1/conversations/{conv}/messages/stream", json={"text": text})
    assert resp.status_code == 200, resp.text
    for block in resp.text.split("\n\n"):
        if block.startswith("event: assistant_message"):
            data = next(line[6:] for line in block.splitlines() if line.startswith("data: "))
            return json.loads(data)["content"]
    raise AssertionError(f"no assistant message in {resp.text!r}")


async def test_chat_pictures_skip_a_frozen_space_and_come_back_with_it(
    web, db_session, qdrant, blobs
):
    w = await _world(db_session, qdrant, blobs)
    org = w.boot.organization_id
    mine = await _personal_of(db_session, w.leaver.id)
    conv = (
        await conversations_service.create_conversation(
            db_session, space_id=mine, organization_id=org, user_id=w.leaver.id, title=None
        )
    ).conversation.id
    await db_session.commit()
    await _as(web, db_session, w.boot, w.leaver.id)
    seen = await _ask(web, conv, "库里有哪些图片")
    assert [m["document_name"] for m in seen["image_matches"]] == ["private.png"]

    await _freeze(db_session, w.boot, w.leaver.id, suspend=False)
    closed = await web.post(
        f"/api/v1/conversations/{conv}/messages/stream", json={"text": "库里有哪些图片"}
    )
    assert closed.status_code == 404 or "event: error" in closed.text
    assert "private.png" not in closed.text

    await _unfreeze(db_session, w.boot, w.leaver.id, restore=False)
    await _as(web, db_session, w.boot, w.leaver.id)
    again = await _ask(web, conv, "库里有哪些图片")
    assert [m["document_name"] for m in again["image_matches"]] == ["private.png"]


# ---------------------------------------------------------------------------
# In-app retrieval
# ---------------------------------------------------------------------------


def _names(result):
    return {c.document_name for c in result.chunks}


async def test_in_app_search_never_returns_a_frozen_space_and_still_finds_shared_content(
    db_session, qdrant, blobs
):
    w = await _world(db_session, qdrant, blobs)
    org = w.boot.organization_id

    async def search(user_id):
        return await retrieve.retrieve_for_user(
            db_session, qdrant=qdrant, organization_id=org, caller_user_id=user_id, query="passage", top_k=8
        )

    assert "private-notes.pdf" in _names(await search(w.leaver.id))  # the setup is searchable
    await _freeze(db_session, w.boot, w.leaver.id)
    for name, user_id in _callers(w).items():
        found = await search(user_id)
        assert "private-notes.pdf" not in _names(found), name
        assert w.private.id not in found.space_ids, name
    # What the person left in a shared space is still found by those who may read it.
    assert "handbook.pdf" in _names(await search(w.colleague.id))

    await _unfreeze(db_session, w.boot, w.leaver.id)
    assert "private-notes.pdf" in _names(await search(w.leaver.id))
    assert "private-notes.pdf" not in _names(await search(w.colleague.id))


async def test_a_conversation_searches_only_what_is_open(db_session, qdrant, blobs):
    w = await _world(db_session, qdrant, blobs)
    org = w.boot.organization_id
    conv = (
        await conversations_service.create_conversation(
            db_session, space_id=w.private.id, organization_id=org, user_id=w.leaver.id, title=None
        )
    ).conversation
    await db_session.commit()
    await _freeze(db_session, w.boot, w.leaver.id, suspend=False)
    conv = await db_session.get(type(conv), conv.id)
    found = await retrieve.retrieve_for_conversation(
        db_session, qdrant=qdrant, conversation=conv, caller_user_id=w.leaver.id, query="passage"
    )
    assert w.private.id not in found.space_ids
    assert "private-notes.pdf" not in _names(found)


# ---------------------------------------------------------------------------
# Outside tools
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["organization owner", "admin", "colleague"])
async def test_outside_tools_see_nothing_of_a_frozen_space(
    http, db_session, redis_client, qdrant, blobs, monkeypatch, who
):
    w = await _world(db_session, qdrant, blobs)
    _strict(monkeypatch)
    tokens = await _read(db_session, redis_client, w.boot, user_id=_callers(w)[who])
    await _freeze(db_session, w.boot, w.leaver.id)
    private_name = (
        await db_session.execute(select(Space.name).where(Space.id == w.private.id))
    ).scalar_one()
    await db_session.commit()

    found = (await http.post(SEARCH, headers=_auth(tokens), json={"query": "passage"})).json()
    assert "private-notes.pdf" not in {p["document_name"] for p in found["passages"]}
    assert "private.png" not in {i["name"] for i in found["images"]}
    pics = (await http.post(IMAGES, headers=_auth(tokens), json={"query": "red"})).json()
    assert "private.png" not in {i["name"] for i in pics["images"]}
    spaces = (await http.get(SPACES, headers=_auth(tokens))).json()
    assert private_name not in {s["name"] for s in spaces["spaces"]}
    assert (await http.get(DOCS, headers=_auth(tokens), params={"space": private_name})).status_code == 404


async def test_the_person_with_an_old_grant_gets_nothing_from_their_frozen_space(
    http, db_session, redis_client, qdrant, blobs
):
    """Only the space is frozen (the person still counts as active): the owner shortcut is closed."""
    w = await _world(db_session, qdrant, blobs)
    tokens = await _read(db_session, redis_client, w.boot, user_id=w.leaver.id)
    private_name = (
        await db_session.execute(select(Space.name).where(Space.id == w.private.id))
    ).scalar_one()
    await db_session.commit()
    before = (await http.post(IMAGES, headers=_auth(tokens), json={"query": "red"})).json()
    link = _path(before["images"][0]["preview_url"])
    assert (await http.get(link)).status_code == 200
    await db_session.commit()

    await _freeze(db_session, w.boot, w.leaver.id, suspend=False)

    assert (await http.get(link)).status_code == 404  # a link handed out earlier stops working
    await db_session.commit()
    found = (await http.post(SEARCH, headers=_auth(tokens), json={"query": "passage"})).json()
    assert "private-notes.pdf" not in {p["document_name"] for p in found["passages"]}
    assert not [i for i in found["images"] if i["name"] == "private.png"]
    pics = (await http.post(IMAGES, headers=_auth(tokens), json={"query": "red"})).json()
    assert pics["images"] == []
    assert (await http.get(DOCS, headers=_auth(tokens), params={"space": private_name})).status_code == 404
    # Saving with no space named must not fall through to some other space.
    saved = await http.post(
        "/api/v1/external/save", headers=_auth(await _read(db_session, redis_client, w.boot, user_id=w.leaver.id, scope="knowledge:read knowledge:write")),
        json={"title": "x", "text": "y"},
    )
    assert saved.status_code >= 400
    assert (
        await db_session.execute(select(func.count()).select_from(Document).where(Document.name.like("x%")))
    ).scalar_one() == 0


async def test_restoring_opens_the_space_again_to_its_owner_only(
    web, db_session, qdrant, blobs, monkeypatch
):
    w = await _world(db_session, qdrant, blobs)
    _strict(monkeypatch)
    await _freeze(db_session, w.boot, w.leaver.id)
    await _unfreeze(db_session, w.boot, w.leaver.id)
    await _as(web, db_session, w.boot, w.leaver.id)
    sid = w.private.id
    assert (await web.get(f"/api/v1/spaces/{sid}")).status_code == 200
    page = (await web.get(f"/api/v1/spaces/{sid}/documents")).json()
    assert {d["name"] for d in page["items"]} == {"private-notes.pdf", "private.png"}
    for who in ("organization owner", "admin", "colleague"):
        await _as(web, db_session, w.boot, _callers(w)[who])
        assert (await web.get(f"/api/v1/spaces/{sid}")).status_code == 404, who


@pytest.mark.parametrize("suspend", [True, False])
async def test_a_document_shared_one_by_one_closes_with_its_frozen_space(
    web, http, db_session, redis_client, qdrant, blobs, suspend
):
    """The owner shared one private document with a colleague, then left."""
    w = await _world(db_session, qdrant, blobs)
    org, sid, doc = w.boot.organization_id, w.private.id, w.private_note
    await _rule(db_session, w.boot, w.private, doc, "allow", "user", w.colleague.id, actor=w.leaver.id)
    tokens = await _read(db_session, redis_client, w.boot, user_id=w.colleague.id)
    await db_session.commit()

    async def doors():
        await _as(web, db_session, w.boot, w.colleague.id)
        listed = (await web.get("/api/v1/spaces")).json()["items"]
        opened = (await web.get(f"/api/v1/spaces/{sid}/documents/{doc.id}")).status_code
        await db_session.commit()
        found = (await http.post(SEARCH, headers=_auth(tokens), json={"query": "passage"})).json()
        await db_session.commit()
        names = {p["document_name"] for p in found["passages"]}
        return {
            "listed": str(sid) in {s["id"] for s in listed},
            "opened": opened == 200,
            "searched": "private-notes.pdf" in names,
            "allowed": await _can(db_session, w.boot, w.colleague, doc),
            "in_app": "private-notes.pdf"
            in _names(
                await retrieve.retrieve_for_user(
                    db_session, qdrant=qdrant, organization_id=org,
                    caller_user_id=w.colleague.id, query="passage", top_k=8,
                )
            ),
        }

    assert await doors() == dict.fromkeys(("listed", "opened", "searched", "allowed", "in_app"), True)
    await _freeze(db_session, w.boot, w.leaver.id, suspend=suspend)
    assert await doors() == dict.fromkeys(("listed", "opened", "searched", "allowed", "in_app"), False)
    await _unfreeze(db_session, w.boot, w.leaver.id, restore=suspend)
    assert await doors() == dict.fromkeys(("listed", "opened", "searched", "allowed", "in_app"), True)


async def test_a_suspended_person_is_shared_nothing_even_in_an_active_space(db_session, qdrant, blobs):
    """The share rules do not depend on the door having turned the person away first."""
    from quberi_api.services import doc_acl

    w = await _world(db_session, qdrant, blobs)
    org = w.boot.organization_id
    other = await _text_doc(db_session, w.boot, qdrant, w.shared, "memo.pdf")
    await _rule(db_session, w.boot, w.shared, other, "allow", "user", w.leaver.id)
    access = await doc_acl.load_doc_access(db_session, user_id=w.leaver.id, organization_id=org)
    assert other.id in access.granted
    assert await _can(db_session, w.boot, w.leaver, other)

    await db_session.commit()
    await db_session.execute(update(User).where(User.id == w.leaver.id).values(status="suspended"))
    await db_session.commit()
    access = await doc_acl.load_doc_access(db_session, user_id=w.leaver.id, organization_id=org)
    assert access.is_empty
    assert not await _can(db_session, w.boot, w.leaver, other)
    # The same rule still serves everyone else it names.
    await _rule(db_session, w.boot, w.shared, other, "allow", "user", w.colleague.id)
    assert await _can(db_session, w.boot, w.colleague, other)


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


async def test_the_space_list_does_not_look_up_the_user_once_per_space(
    web, db_session, qdrant, blobs
):
    from sqlalchemy import event

    w = await _world(db_session, qdrant, blobs)
    await _as(web, db_session, w.boot, w.colleague.id)

    async def user_lookups():
        seen: list[str] = []

        def _record(conn, cursor, statement, *_):
            seen.append(statement)

        bind = db_session.sync_session.get_bind()
        event.listen(bind, "before_cursor_execute", _record)
        try:
            assert (await web.get("/api/v1/spaces")).status_code == 200
        finally:
            event.remove(bind, "before_cursor_execute", _record)
        await db_session.commit()
        return len([s for s in seen if "FROM users" in s and "users.id =" in s and "users.status" in s])

    few = await user_lookups()
    for n in range(3):
        extra = await _make_space(db_session, w.boot, f"More {n}")
        await _grant(db_session, w.boot, SimpleNamespace(id=extra.id), SimpleNamespace(id=w.colleague.id), "viewer")
    await _as(web, db_session, w.boot, w.colleague.id)
    many = await user_lookups()
    assert many == few
