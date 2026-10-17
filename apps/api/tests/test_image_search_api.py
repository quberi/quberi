"""Picture search in the chat, and the thumbnails its results use, over HTTP.

The routers are mounted as in production with the caller fixed by a dependency override;
the database, the permission evaluator, the picture vector store (qdrant-client's in-memory
engine) and the conversation service are real. The image model and MinIO are stood in for.
"""

from __future__ import annotations

import io
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy import select

from quberi_api.api import dependencies
from quberi_api.api.errors import register_exception_handlers
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.api.v1.conversations import router as conv_router
from quberi_api.api.v1.spaces import documents as documents_router
from quberi_api.core.config import get_settings
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.conversations.models import Conversation, Message
from quberi_api.domain.identity.models import Organization, User
from quberi_api.domain.spaces.models import Space
from quberi_api.services import conversations as conversations_service
from quberi_api.services import members as members_service
from quberi_api.services import space_bindings as space_bindings_service
from quberi_api.services.ai.local_image_embedding import SIGLIP2_DIM
from quberi_api.services.documents import thumbnail as thumbnail_module
from quberi_api.services.rag import image_search
from quberi_api.services.rag.image_store import ImageVectorStore
from tests.test_image_indexing import _doc, _space
from tests.test_image_search import GREEN, RED, FakeEmbedder

MODEL = "siglip2-base"


def _png(colour="red", size=(40, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
async def qdrant(monkeypatch):
    client = AsyncQdrantClient(":memory:")

    class Shared:
        def __getattr__(self, name):
            return getattr(client, name)

        async def close(self):
            return None

    # The conversation service opens its own client; hand it the shared in-memory one.
    monkeypatch.setattr(conversations_service, "create_qdrant_client", lambda _s: Shared())
    yield client
    await client.close()


@pytest.fixture
async def redis_client():
    client = Redis.from_url("redis://127.0.0.1:6379/1", decode_responses=True)
    for key in await client.keys("quberi:imagesearch:*"):
        await client.delete(key)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    monkeypatch.setattr(image_search, "get_image_embedder", lambda *_a, **_k: FakeEmbedder())


class Who:
    """The caller the app sees; switch ``user`` between requests."""

    def __init__(self, user, organization):
        self.user, self.organization = user, organization


@pytest.fixture
async def client(db_session, org_and_owner, redis_client):
    user = await db_session.get(User, org_and_owner.user_id)
    org = await db_session.get(Organization, org_and_owner.organization_id)
    await db_session.commit()
    who = Who(user, org)
    app = FastAPI()
    app.state.settings = get_settings()
    register_exception_handlers(app)
    app.include_router(conv_router.conversation_router, prefix="/api/v1")
    app.include_router(documents_router.router, prefix="/api/v1")

    async def _session():
        yield db_session

    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[dependencies.get_infrastructure] = lambda: SimpleNamespace(
        redis=redis_client, qdrant=object()
    )
    app.dependency_overrides[require_caller] = lambda: CallerContext(
        user=who.user, organization=who.organization, is_platform_admin=False
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        http.who = who  # type: ignore[attr-defined]
        yield http


async def _conversation(db, boot):
    view = await conversations_service.create_conversation(
        db,
        space_id=boot.personal_space_id,
        organization_id=boot.organization_id,
        user_id=boot.user_id,
        title=None,
    )
    await db.commit()
    return view.conversation


async def _picture(db, boot, qdrant, space, vec, name="part.png"):
    doc = await _doc(db, boot, space, name, state="indexed")
    await ImageVectorStore(qdrant, model_id=MODEL, vector_dim=SIGLIP2_DIM).upsert_image(
        organization_id=boot.organization_id, space_id=space.id, document_id=doc.id,
        document_name=name, mime_type="image/png", document_checksum=None, vector=vec,
    )
    return doc


def _matches(resp) -> list[dict]:
    return resp.json()["assistant_message"]["content"]["image_matches"]


async def _messages_in(db, conv):
    return (
        await db.execute(select(Message).where(Message.conversation_id == conv.id))
    ).first()


def _url(conv) -> str:
    return f"/api/v1/conversations/{conv.id}/image-search"


async def _post(http, conv, *, file=None, text=None, top_k=None):
    files = {"file": ("query.png", file, "image/png")} if file is not None else None
    data = {}
    if text is not None:
        data["text"] = text
    if top_k is not None:
        data["top_k"] = str(top_k)
    return await http.post(_url(conv), files=files, data=data)


# ---------------------------------------------------------------------------
# The chat endpoint
# ---------------------------------------------------------------------------


async def test_a_picture_finds_similar_pictures_and_the_answer_is_a_chat_message(
    client, db_session, org_and_owner, qdrant
):
    space = await _space(db_session, org_and_owner, "Parts")
    red = await _picture(db_session, org_and_owner, qdrant, space, RED, "red.png")
    await _picture(db_session, org_and_owner, qdrant, space, GREEN, "green.png")
    conv = await _conversation(db_session, org_and_owner)

    resp = await _post(client, conv, file=_png("red"))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    answer = body["assistant_message"]["content"]
    assert answer["text"] == "找到 2 张相似的图片。"
    first = answer["image_matches"][0]
    assert first["document_id"] == str(red.id)
    assert first["document_name"] == "red.png"
    assert first["space_name"] == "Parts"
    assert first["mime_type"] == "image/png"
    assert first["score"] > answer["image_matches"][1]["score"]
    assert answer["meta"]["kind"] == "image_search"
    assert body["conversation"]["message_count"] == 2
    assert body["user_message"]["role"] == "user"
    assert body["user_message"]["content"]["image_query"] == {"picture": True, "description": False}


async def test_words_alone_find_pictures_too(client, db_session, org_and_owner, qdrant):
    space = await _space(db_session, org_and_owner, "Parts")
    await _picture(db_session, org_and_owner, qdrant, space, GREEN, "green.png")
    conv = await _conversation(db_session, org_and_owner)

    resp = await _post(client, conv, text="a green part")

    assert resp.status_code == 201, resp.text
    assert _matches(resp)[0]["document_name"] == "green.png"
    assert resp.json()["user_message"]["content"]["image_query"] == {
        "picture": False, "description": True
    }


async def test_the_query_picture_is_not_kept_anywhere(client, db_session, org_and_owner, qdrant):
    import base64

    space = await _space(db_session, org_and_owner, "Parts")
    await _picture(db_session, org_and_owner, qdrant, space, RED)
    conv = await _conversation(db_session, org_and_owner)
    data = _png("red", size=(33, 21))

    resp = await _post(client, conv, file=data, text="my secret description")

    assert resp.status_code == 201
    stored = (
        (
            await db_session.execute(
                select(Message.content).where(Message.conversation_id == conv.id)
            )
        )
        .scalars()
        .all()
    )
    dumped = repr(stored) + repr(
        (await db_session.execute(select(AuditEvent.payload))).scalars().all()
    )
    assert base64.b64encode(data[:24]).decode() not in dumped
    assert "query.png" not in dumped  # not even the file name
    assert repr(data[:16]) not in dumped
    # The words the person typed are in their own message but never in the audit log.
    audit = (
        await db_session.execute(
            select(AuditEvent.payload).where(AuditEvent.action == "message.sent")
        )
    ).scalars().all()
    assert "my secret description" not in repr(audit)
    assert audit[-1]["kind"] == "image_search"
    assert audit[-1]["query"] == {"picture": True, "description": True}


async def test_the_search_is_audited_with_document_ids_only(
    client, db_session, org_and_owner, qdrant
):
    space = await _space(db_session, org_and_owner, "Parts")
    red = await _picture(db_session, org_and_owner, qdrant, space, RED, "secret-name.png")
    conv = await _conversation(db_session, org_and_owner)

    await _post(client, conv, file=_png("red"))

    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.action == "message.sent", AuditEvent.resource_id == conv.id
            )
        )
    ).scalar_one()
    assert event.payload["retrieved_document_ids"] == [str(red.id)]
    assert "secret-name.png" not in repr(event.payload)


async def test_it_looks_only_where_the_conversation_may_look(
    client, db_session, org_and_owner, qdrant
):
    mine = await _space(db_session, org_and_owner, "Mine")
    other = await _space(db_session, org_and_owner, "Other")
    await _picture(db_session, org_and_owner, qdrant, mine, RED, "in-mine.png")
    await _picture(db_session, org_and_owner, qdrant, other, RED, "in-other.png")
    conv = await _conversation(db_session, org_and_owner)
    await conversations_service.set_scope_mode(
        db_session, conversation_id=conv.id, organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id, mode="custom",
    )
    await conversations_service.remove_scope_source(
        db_session, conversation_id=conv.id, organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id, space_id=other.id,
    )

    resp = await _post(client, conv, file=_png("red"))

    names = [m["document_name"] for m in _matches(resp)]
    assert names == ["in-mine.png"]
    assert resp.json()["assistant_message"]["content"]["meta"]["scope_mode"] == "custom"


async def test_a_space_you_cannot_read_never_shows_up(client, db_session, org_and_owner, qdrant):
    hidden = await _space(db_session, org_and_owner, "Hidden")
    await db_session.commit()
    invited = await members_service.invite_member(
        db_session, organization_id=org_and_owner.organization_id,
        invited_by_user_id=org_and_owner.user_id, email=f"m-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Member", role_code="member",
    )
    member = await db_session.get(User, invited.user.id)
    member.status = "active"
    await db_session.flush()
    await db_session.commit()
    await _picture(db_session, org_and_owner, qdrant, hidden, RED, "hidden.png")
    personal = (
        await db_session.execute(
            select(Space.id).where(Space.owner_id == member.id, Space.preset == "personal")
        )
    ).scalar_one()
    await db_session.commit()
    member_conv = (
        await conversations_service.create_conversation(
            db_session, space_id=personal,
            organization_id=org_and_owner.organization_id, user_id=member.id, title=None,
        )
    ).conversation
    await db_session.commit()
    client.who.user = member

    resp = await _post(client, member_conv, file=_png("red"))

    assert resp.status_code == 201
    content = resp.json()["assistant_message"]["content"]
    assert content["image_matches"] == []
    assert "没有找到相似的图片" in content["text"]
    assert "hidden.png" not in resp.text


async def test_a_chosen_space_you_have_since_lost_is_dropped_from_a_custom_scope(
    client, db_session, org_and_owner, qdrant
):
    from tests.test_image_search import _grant, _member, _revoke

    space = await _space(db_session, org_and_owner, "Parts")
    member = await _member(db_session, org_and_owner)
    await _grant(db_session, org_and_owner, space, member)
    await _picture(db_session, org_and_owner, qdrant, space, RED, "parts.png")
    personal = (
        await db_session.execute(
            select(Space.id).where(Space.owner_id == member.id, Space.preset == "personal")
        )
    ).scalar_one()
    await db_session.commit()
    conv = (
        await conversations_service.create_conversation(
            db_session, space_id=personal, organization_id=org_and_owner.organization_id,
            user_id=member.id, title=None,
        )
    ).conversation
    await db_session.commit()
    await conversations_service.set_scope_mode(
        db_session, conversation_id=conv.id, organization_id=org_and_owner.organization_id,
        caller_user_id=member.id, mode="custom",
    )
    client.who.user = member

    before = await _post(client, conv, file=_png("red"))
    await db_session.commit()
    await _revoke(db_session, org_and_owner, space, member)
    after = await _post(client, conv, file=_png("red"))

    found = [m["document_name"] for m in _matches(before)]
    assert found == ["parts.png"]
    # The space is still ticked in the conversation's list, but the person can no longer read it.
    assert after.json()["assistant_message"]["content"]["image_matches"] == []


async def test_someone_elses_conversation_is_not_found(client, db_session, org_and_owner, qdrant):
    conv = await _conversation(db_session, org_and_owner)
    stranger = SimpleNamespace(id=uuid.uuid4(), identity_id=uuid.uuid4())
    client.who.user = stranger
    resp = await _post(client, conv, file=_png("red"))
    assert resp.status_code == 404
    assert await _messages_in(db_session, conv) is None


async def test_nothing_is_written_when_the_query_is_refused(
    client, db_session, org_and_owner, qdrant
):
    conv = await _conversation(db_session, org_and_owner)

    for kwargs, code in [
        ({"file": b"this is not a picture"}, 400),
        ({}, 400),  # no picture, no words
        ({"text": "   "}, 400),
        ({"file": _png("red"), "text": "x" * 501}, 422),  # over the form limit
    ]:
        resp = await _post(client, conv, **kwargs)
        assert resp.status_code == code, (kwargs.keys(), resp.text)
        await db_session.commit()

    assert await _messages_in(db_session, conv) is None
    refreshed = await db_session.get(Conversation, conv.id)
    await db_session.refresh(refreshed)
    assert refreshed.message_count == 0


async def test_an_oversized_picture_is_refused_while_reading(
    client, db_session, org_and_owner, qdrant, monkeypatch
):
    monkeypatch.setattr(conv_router, "_MAX_QUERY_PICTURE_BYTES", 1024)
    conv = await _conversation(db_session, org_and_owner)
    resp = await _post(client, conv, file=b"\x89PNG\r\n\x1a\n" + b"0" * 5000)
    assert resp.status_code == 413


async def test_a_switched_off_server_says_so(
    client, db_session, org_and_owner, qdrant, monkeypatch
):
    monkeypatch.setattr(get_settings(), "image_search_enabled", False)
    conv = await _conversation(db_session, org_and_owner)
    resp = await _post(client, conv, file=_png("red"))
    assert resp.status_code == 503
    assert "turned off" in resp.json()["detail"]


async def test_picture_searches_are_rate_limited_per_person(
    client, db_session, org_and_owner, qdrant, monkeypatch
):
    monkeypatch.setattr(conv_router, "_IMAGE_SEARCH_PER_MINUTE", 2)
    conv = await _conversation(db_session, org_and_owner)
    assert (await _post(client, conv, text="a red part")).status_code == 201
    assert (await _post(client, conv, text="a red part")).status_code == 201
    third = await _post(client, conv, text="a red part")
    assert third.status_code == 429
    assert third.headers["retry-after"] == "60"


async def test_the_result_count_is_limited(client, db_session, org_and_owner, qdrant):
    space = await _space(db_session, org_and_owner, "Parts")
    for i in range(4):
        await _picture(db_session, org_and_owner, qdrant, space, RED, f"{i}.png")
    conv = await _conversation(db_session, org_and_owner)

    two = await _post(client, conv, file=_png("red"), top_k=2)
    assert len(two.json()["assistant_message"]["content"]["image_matches"]) == 2
    assert (await _post(client, conv, file=_png("red"), top_k=31)).status_code == 422
    assert (await _post(client, conv, file=_png("red"), top_k=0)).status_code == 422


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------


@pytest.fixture
def blobs(monkeypatch):
    from quberi_api.services.documents import core as documents_core

    store: dict[str, bytes] = {}

    def fake_open(storage_key):
        async def stream():
            data = store[storage_key]
            for i in range(0, len(data), 4096):
                yield data[i : i + 4096]

        return stream()

    monkeypatch.setattr(documents_core.local_connector, "open_stream", fake_open)
    return store


def _thumb(space, doc) -> str:
    return f"/api/v1/spaces/{space.id}/documents/{doc.id}/thumbnail"


async def _stored_picture(db, boot, space, blobs, data, name="a.png", mime="image/png"):
    doc = await _doc(db, boot, space, name, mime=mime, state="indexed", blobs=blobs, data=data)
    return doc


async def test_a_thumbnail_is_a_small_plain_jpeg(client, db_session, org_and_owner, blobs):
    space = await _space(db_session, org_and_owner, "Parts")
    doc = await _stored_picture(db_session, org_and_owner, space, blobs, _png("blue", (1600, 900)))

    resp = await client.get(_thumb(space, doc))

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["cache-control"].startswith("private")
    image = Image.open(io.BytesIO(resp.content))
    assert image.format == "JPEG" and max(image.size) <= 320
    assert len(resp.content) < 20_000


async def test_a_thumbnail_follows_the_download_permission(
    client, db_session, org_and_owner, blobs
):
    space = await _space(db_session, org_and_owner, "Parts")
    doc = await _stored_picture(db_session, org_and_owner, space, blobs, _png())
    await db_session.commit()
    invited = await members_service.invite_member(
        db_session, organization_id=org_and_owner.organization_id,
        invited_by_user_id=org_and_owner.user_id, email=f"m-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Member", role_code="member",
    )
    member = await db_session.get(User, invited.user.id)
    member.status = "active"
    await db_session.flush()
    await db_session.commit()
    client.who.user = member

    denied = await client.get(_thumb(space, doc))
    assert denied.status_code == 404
    await db_session.commit()

    await space_bindings_service.add_binding(
        db_session, space_id=space.id, organization_id=org_and_owner.organization_id,
        actor_user_id=org_and_owner.user_id, subject_type="user", subject_id=member.id,
        role_code="viewer",
    )
    await db_session.commit()
    assert (await client.get(_thumb(space, doc))).status_code == 200


async def test_only_pictures_have_thumbnails(client, db_session, org_and_owner, blobs):
    space = await _space(db_session, org_and_owner, "Parts")
    doc = await _stored_picture(
        db_session, org_and_owner, space, blobs, b"%PDF-1.7 hello", "a.pdf", "application/pdf"
    )
    assert (await client.get(_thumb(space, doc))).status_code == 404


async def test_a_file_that_only_claims_to_be_a_picture_is_never_passed_on(
    client, db_session, org_and_owner, blobs
):
    space = await _space(db_session, org_and_owner, "Parts")
    script = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    doc = await _stored_picture(
        db_session, org_and_owner, space, blobs, script, "x.png", "image/png"
    )

    resp = await client.get(_thumb(space, doc))

    assert resp.status_code == 415
    assert b"script" not in resp.content


async def test_a_thumbnail_from_the_wrong_space_is_not_found(
    client, db_session, org_and_owner, blobs
):
    a = await _space(db_session, org_and_owner, "A")
    b = await _space(db_session, org_and_owner, "B")
    doc = await _stored_picture(db_session, org_and_owner, a, blobs, _png())
    assert (await client.get(_thumb(b, doc))).status_code == 404


async def test_an_oversized_source_is_not_decoded(
    client, db_session, org_and_owner, blobs, monkeypatch
):
    monkeypatch.setattr(thumbnail_module, "MAX_SOURCE_BYTES", 2048)
    space = await _space(db_session, org_and_owner, "Parts")
    doc = await _stored_picture(
        db_session, org_and_owner, space, blobs, b"\x89PNG\r\n\x1a\n" + b"0" * 9000
    )
    assert (await client.get(_thumb(space, doc))).status_code == 413


async def test_the_space_list_can_be_narrowed_to_one_kind(client, db_session, org_and_owner):
    space = await _space(db_session, org_and_owner, "Mixed")
    await _doc(db_session, org_and_owner, space, "a.png", mime="image/png", state="indexed")
    await _doc(db_session, org_and_owner, space, "b.pdf", mime="application/pdf", state="indexed")
    url = f"/api/v1/spaces/{space.id}/documents"

    images = (await client.get(url, params={"file_type": "image"})).json()
    everything = (await client.get(url)).json()
    bad = await client.get(url, params={"file_type": "videos"})

    assert [d["name"] for d in images["items"]] == ["a.png"]
    assert images["total"] == 1 and images["filters"]["file_type"] == "image"
    assert everything["total"] == 2 and everything["filters"]["file_type"] is None
    assert bad.status_code == 422
