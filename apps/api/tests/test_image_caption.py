"""A vision model describes a picture so what it shows can be searched.

What matters here is what is sent, to whom and when: nothing leaves unless an owner or admin
chose a model *and* turned descriptions on, and every other condition (approved, not archived,
active space, not a guest upload, under the daily limit) holds. The model, the embedding model
and Redis are stood in for; the database, the services and the vector store are real.
"""

from __future__ import annotations

import base64
import io
import json
import uuid
from types import SimpleNamespace

import pytest
from PIL import Image
from qdrant_client import AsyncQdrantClient
from sqlalchemy import select, update

from quberi_api.core.config import get_settings
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document
from quberi_api.domain.identity.models import Organization
from quberi_api.domain.spaces.models import Space
from quberi_api.services.ai import ChatMessage, ImagePart
from quberi_api.services.ai.base import ChatResponse
from quberi_api.services.ai.openai_compat import OpenAICompatProvider
from quberi_api.services.ai.registry import MODEL_REGISTRY, ModelSpec
from quberi_api.services.rag import image_caption
from quberi_api.services.rag.vector_store import VectorStore
from tests.test_image_indexing import _doc, _png, _space

VISION_MODEL = "deepseek-flash"
EMBED_MODEL = "test-embed"
DIM = 8


# --------------------------------------------------------------------------
# What is sent: the picture, and the message that carries it
# --------------------------------------------------------------------------


def _image(fmt, size=(300, 200), **kw):
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, format=fmt, **kw)
    return buf.getvalue()


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF", "WEBP"])
def test_the_accepted_formats_become_a_fresh_jpeg(fmt):
    part = image_caption.prepare_for_model(_image(fmt))
    assert part.mime_type == "image/jpeg" and part.data[:3] == b"\xff\xd8\xff"


def test_a_large_picture_is_scaled_down_before_it_is_sent():
    part = image_caption.prepare_for_model(_image("PNG", size=(5000, 3000)))
    with Image.open(io.BytesIO(part.data)) as sent:
        assert max(sent.size) == image_caption.SEND_MAX_SIDE


@pytest.mark.parametrize(
    "data,why",
    [
        (b"", "empty"),
        (b"not a picture at all", "not a readable picture"),
        (_image("TIFF"), "only PNG, JPEG, GIF and WebP"),
        (_image("BMP"), "only PNG, JPEG, GIF and WebP"),
    ],
)
def test_what_is_not_a_picture_the_model_accepts_is_refused(data, why):
    with pytest.raises(image_caption.CaptionError, match=why):
        image_caption.prepare_for_model(data)


def test_a_picture_claiming_too_many_pixels_is_refused_before_it_is_decoded(monkeypatch):
    monkeypatch.setattr(image_caption, "MAX_SEND_PIXELS", 100)
    with pytest.raises(image_caption.CaptionError, match="too many pixels"):
        image_caption.prepare_for_model(_image("PNG", size=(20, 20)))


def test_a_user_message_carries_its_picture_as_a_data_url():
    part = ImagePart("image/jpeg", b"\xff\xd8\xffabc")
    sent = OpenAICompatProvider._serialize_message(
        ChatMessage(role="user", content="describe", images=(part,))
    )

    assert sent["role"] == "user"
    text, picture = sent["content"]
    assert text == {"type": "text", "text": "describe"}
    assert picture["type"] == "image_url"
    url = picture["image_url"]["url"]
    assert url == "data:image/jpeg;base64," + base64.b64encode(part.data).decode()
    assert "detail" not in picture["image_url"]  # never the low-resolution setting


@pytest.mark.parametrize("role", ["system", "assistant", "tool"])
def test_a_picture_in_any_other_role_is_refused_not_sent(role):
    with pytest.raises(ValueError, match="user message"):
        OpenAICompatProvider._serialize_message(
            ChatMessage(role=role, content="x", images=(ImagePart("image/png", b"x"),))
        )


def test_a_message_without_pictures_is_serialised_exactly_as_before():
    assert OpenAICompatProvider._serialize_message(ChatMessage(role="user", content="hi")) == {
        "role": "user",
        "content": "hi",
    }


def test_the_answer_is_plain_bounded_text():
    assert image_caption.clean_caption("  一张\n\n红色  齿轮箱  ") == "一张 红色 齿轮箱"
    long = image_caption.clean_caption("字" * 5000)
    assert len(long) == image_caption.MAX_CAPTION_CHARS + 1 and long.endswith("…")
    assert image_caption.clean_caption(None) == ""  # type: ignore[arg-type]


def test_only_models_that_read_pictures_are_candidates():
    assert {m.model_id for m in MODEL_REGISTRY.values() if m.supports_vision} >= {
        "deepseek-flash",
        "gpt-4o",
        "gpt-4o-mini",
        "qwen-vl-max",
    }
    assert not MODEL_REGISTRY["deepseek-chat"].supports_vision
    assert not MODEL_REGISTRY["bge-m3"].supports_vision


# --------------------------------------------------------------------------
# The job
# --------------------------------------------------------------------------


class Vision:
    """The vision model: records what it is sent."""

    def __init__(self, answer="一张红色齿轮箱的照片，铭牌上写着 HZ-8800B。", error=None):
        self.answer, self.error, self.calls = answer, error, []

    async def complete(self, *, messages, model, temperature=0.2, max_tokens=None, **_kw):
        self.calls.append(SimpleNamespace(messages=list(messages), model=model))
        if self.error:
            raise self.error
        return ChatResponse(content=self.answer)


class Embedder:
    vector_dim = DIM

    async def embed(self, texts, **_kw):
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]


class FakeRedis:
    def __init__(self):
        self.counts, self.expiry = {}, {}

    async def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key, seconds):
        self.expiry[key] = seconds


@pytest.fixture
def world(monkeypatch):
    """The stood-in outside world: a vision model, an embedding model, Redis, a file store."""
    vision = Vision()
    state = SimpleNamespace(
        vision=vision,
        redis=FakeRedis(),
        chosen=True,  # a vision model is chosen
        embedding_model=EMBED_MODEL,
        loads=[],
    )

    async def resolve_vision(_session, *, organization_id):
        if not state.chosen:
            return None
        return SimpleNamespace(
            provider=state.vision,
            model=MODEL_REGISTRY[VISION_MODEL],
            binding_id=uuid.UUID(int=7),
        )

    async def resolve_embedding(_session, *, organization_id):
        return SimpleNamespace(
            provider=Embedder(),
            model=SimpleNamespace(model_id=state.embedding_model, vector_dim=DIM),
        )

    async def load(_key):
        state.loads.append(_key)
        return _png("red")

    state.load = load
    monkeypatch.setattr(image_caption, "resolve_vision_provider", resolve_vision)
    monkeypatch.setattr(image_caption, "resolve_embedding_provider", resolve_embedding)
    return state


@pytest.fixture
async def qdrant():
    client = AsyncQdrantClient(":memory:")
    yield client
    await client.close()


async def _enable(db, boot, on=True):
    await db.execute(
        update(Organization)
        .where(Organization.id == boot.organization_id)
        .values(image_vision_enabled=on)
    )
    await db.commit()


async def _picture(db, boot, space=None, name="gearbox.png", **values):
    space = space or await _space(db, boot)
    doc = await _doc(db, boot, space, name, state="indexed")
    await db.execute(
        update(Document).where(Document.id == doc.id).values({"review_state": "approved", **values})
    )
    await db.commit()
    return SimpleNamespace(id=doc.id, space_id=space.id, checksum=doc.checksum_sha256)


async def _run(db, qdrant, world, pic, **kw):
    return await image_caption.index_image_caption(
        db, qdrant=qdrant, redis=world.redis, document_id=pic.id, blob_loader=world.load, **kw
    )


async def _points(qdrant):
    store = VectorStore(qdrant, model_id=EMBED_MODEL, vector_dim=DIM)
    if not await qdrant.collection_exists(store.collection):
        return []
    found, _ = await qdrant.scroll(store.collection, limit=100, with_payload=True)
    return sorted((p.payload for p in found), key=lambda p: p["chunk_index"])


async def test_a_picture_is_described_filed_and_audited_without_its_content(
    db_session, org_and_owner, qdrant, world
):
    await _enable(db_session, org_and_owner)
    pic = await _picture(db_session, org_and_owner)

    outcome = await _run(db_session, qdrant, world, pic)

    assert (outcome.status, outcome.called_model) == ("indexed", True)
    # The model got one request: a text-only system message and a user message with the picture.
    (call,) = world.vision.calls
    system, user = call.messages
    assert system.role == "system" and not system.images
    assert user.role == "user" and len(user.images) == 1
    assert user.images[0].mime_type == "image/jpeg"
    assert call.model == VISION_MODEL
    # Filed in the text index, as a description (never as text read out of the picture).
    (point,) = await _points(qdrant)
    assert point["source_kind"] == image_caption.CAPTION_SOURCE_KIND
    assert point["chunk_index"] >= image_caption.CAPTION_CHUNK_BASE
    assert "齿轮箱" in point["text"] and "模型自动描述" in point["text"]
    assert point["document_id"] == str(pic.id) and point["space_id"] == str(pic.space_id)
    # The model's reading of a part number is not evidence of one: no exact-match keys.
    assert "codes" not in point
    # Audited: that a picture was sent and to which model; never the picture or the answer.
    await db_session.commit()
    (event,) = (
        (
            await db_session.execute(
                select(AuditEvent).where(AuditEvent.action == "image.vision_called")
            )
        )
        .scalars()
        .all()
    )
    assert event.resource_id == pic.id
    assert event.payload["model_id"] == VISION_MODEL
    assert "齿轮箱" not in json.dumps(event.payload, ensure_ascii=False)
    # The state says what it was made from.
    row = (await db_session.execute(select(Document).where(Document.id == pic.id))).scalar_one()
    state = row.attributes[image_caption.IMAGE_CAPTION_KEY]
    assert (state["status"], state["source_checksum"], state["vision_model"]) == (
        "indexed",
        pic.checksum,
        VISION_MODEL,
    )


async def test_the_description_and_the_text_read_out_of_the_picture_do_not_erase_each_other(
    db_session, org_and_owner, qdrant, world, monkeypatch
):
    from quberi_api.services.ai.ocr import OcrLine, OcrResult
    from quberi_api.services.ai.ocr.models import ENGINE_NAME, ENGINE_VERSION, MODEL_NAME
    from quberi_api.services.rag import image_text

    class Ocr:
        async def recognize(self, image):
            line = OcrLine("HZ-8800B", 0.95, ((0, 0), (100, 0), (100, 20), (0, 20)))
            return OcrResult(
                lines=[line],
                engine=ENGINE_NAME,
                model=MODEL_NAME,
                version=ENGINE_VERSION,
                width=100,
                height=100,
            )

    async def resolve_embedding(_session, *, organization_id):
        return SimpleNamespace(
            provider=Embedder(), model=SimpleNamespace(model_id=EMBED_MODEL, vector_dim=DIM)
        )

    monkeypatch.setattr(image_text, "resolve_embedding_provider", resolve_embedding)
    monkeypatch.setattr(image_text, "get_ocr_provider", lambda: Ocr())
    await _enable(db_session, org_and_owner)
    pic = await _picture(db_session, org_and_owner)

    await image_text.index_image_text(
        db_session, qdrant=qdrant, document_id=pic.id, blob_loader=world.load
    )
    await _run(db_session, qdrant, world, pic)
    kinds = sorted(p["source_kind"] for p in await _points(qdrant))
    assert kinds == ["image_caption", "image_ocr"]

    # Reading the text again keeps the description; describing again keeps the text.
    await image_text.index_image_text(
        db_session, qdrant=qdrant, document_id=pic.id, blob_loader=world.load, force=True
    )
    assert sorted(p["source_kind"] for p in await _points(qdrant)) == kinds
    await _run(db_session, qdrant, world, pic, force=True)
    assert sorted(p["source_kind"] for p in await _points(qdrant)) == kinds
    codes = [p.get("codes") for p in await _points(qdrant) if p["source_kind"] == "image_caption"]
    assert codes == [None]


@pytest.mark.parametrize(
    "why,setup",
    [
        ("off", "switch_off"),
        ("no model", "no_model"),
        ("not approved", "pending"),
        ("archived", "archived"),
        ("no stored file", "no_file"),
        ("guest upload", "guest"),
        ("not a picture", "not_picture"),
        ("space not active", "space_frozen"),
    ],
)
async def test_nothing_is_sent_unless_every_condition_holds(
    db_session, org_and_owner, qdrant, world, why, setup
):
    await _enable(db_session, org_and_owner, on=setup != "switch_off")
    pic = await _picture(db_session, org_and_owner)
    changes = {
        "pending": {"review_state": "pending"},
        "archived": {"status": "archived"},
        "no_file": {"storage_key": None},
        "guest": {"attributes": {"guest_owner": "abc"}},
        "not_picture": {"mime_type": "application/pdf"},
    }
    if setup in changes:
        await db_session.execute(
            update(Document).where(Document.id == pic.id).values(**changes[setup])
        )
    if setup == "space_frozen":
        await db_session.execute(
            update(Space).where(Space.id == pic.space_id).values(status="archived")
        )
    world.chosen = setup != "no_model"
    await db_session.commit()

    outcome = await _run(db_session, qdrant, world, pic)

    assert outcome.status == "skipped", why
    assert world.vision.calls == [] and world.loads == []  # not even read from storage
    assert await _points(qdrant) == []
    assert world.redis.counts == {}  # and it did not count against the limit


async def test_the_daily_limit_stops_the_calls(
    db_session, org_and_owner, qdrant, world, monkeypatch
):
    monkeypatch.setattr(get_settings(), "vision_daily_limit", 2)
    await _enable(db_session, org_and_owner)
    space = await _space(db_session, org_and_owner)
    first, second, third = [
        await _picture(db_session, org_and_owner, space, f"p{i}.png") for i in range(3)
    ]

    results = [await _run(db_session, qdrant, world, p) for p in (first, second, third)]

    assert [r.status for r in results] == ["indexed", "indexed", "skipped"]
    assert "limit" in results[2].reason
    assert len(world.vision.calls) == 2
    (key,) = world.redis.counts
    assert str(org_and_owner.organization_id) in key and world.redis.expiry[key] > 24 * 3600


async def test_a_limit_of_zero_means_nothing_is_ever_sent(
    db_session, org_and_owner, qdrant, world, monkeypatch
):
    monkeypatch.setattr(get_settings(), "vision_daily_limit", 0)
    await _enable(db_session, org_and_owner)
    pic = await _picture(db_session, org_and_owner)

    assert (await _run(db_session, qdrant, world, pic)).status == "skipped"
    assert world.vision.calls == []


async def test_a_description_already_made_for_this_file_and_model_is_not_made_again(
    db_session, org_and_owner, qdrant, world
):
    await _enable(db_session, org_and_owner)
    pic = await _picture(db_session, org_and_owner)
    await _run(db_session, qdrant, world, pic)

    again = await _run(db_session, qdrant, world, pic)

    assert (again.status, again.reason) == ("skipped", "already up to date")
    assert len(world.vision.calls) == 1


async def test_a_new_embedding_model_refiles_the_saved_description_without_calling_the_model(
    db_session, org_and_owner, qdrant, world
):
    await _enable(db_session, org_and_owner)
    pic = await _picture(db_session, org_and_owner)
    await _run(db_session, qdrant, world, pic)
    world.embedding_model = "another-embed"

    again = await _run(db_session, qdrant, world, pic)

    assert (again.status, again.called_model) == ("indexed", False)
    assert len(world.vision.calls) == 1  # nothing more was sent
    store = VectorStore(qdrant, model_id="another-embed", vector_dim=DIM)
    assert await qdrant.collection_exists(store.collection)


async def test_a_failed_call_files_nothing_and_leaves_no_state_so_it_is_tried_again(
    db_session, org_and_owner, qdrant, world
):
    world.vision.error = RuntimeError("upstream 500: secret-key-in-message")
    await _enable(db_session, org_and_owner)
    pic = await _picture(db_session, org_and_owner)

    outcome = await _run(db_session, qdrant, world, pic)

    assert outcome.status == "failed" and outcome.called_model
    assert "secret-key" not in (outcome.reason or "")  # the provider's message is not repeated
    assert await _points(qdrant) == []
    row = (await db_session.execute(select(Document).where(Document.id == pic.id))).scalar_one()
    assert image_caption.IMAGE_CAPTION_KEY not in (row.attributes or {})


async def test_an_empty_answer_is_a_failure_not_an_empty_description(
    db_session, org_and_owner, qdrant, world
):
    world.vision.answer = "   "
    await _enable(db_session, org_and_owner)
    pic = await _picture(db_session, org_and_owner)

    assert (await _run(db_session, qdrant, world, pic)).status == "failed"
    assert await _points(qdrant) == []


async def test_a_file_that_is_not_really_a_picture_is_not_sent_and_not_retried(
    db_session, org_and_owner, qdrant, world
):
    await _enable(db_session, org_and_owner)
    pic = await _picture(db_session, org_and_owner)

    async def junk(_key):
        return b"MZ not a picture"

    outcome = await image_caption.index_image_caption(
        db_session, qdrant=qdrant, redis=world.redis, document_id=pic.id, blob_loader=junk
    )

    assert outcome.status == "failed" and world.vision.calls == []
    row = (await db_session.execute(select(Document).where(Document.id == pic.id))).scalar_one()
    assert row.attributes[image_caption.IMAGE_CAPTION_KEY]["status"] == "failed"


async def test_the_description_is_hidden_from_whoever_the_picture_is_hidden_from(
    db_session, org_and_owner, qdrant, world
):
    """Filed under the picture's document and space, so the one access rule covers it."""
    await _enable(db_session, org_and_owner)
    shown = await _picture(db_session, org_and_owner, name="shown.png")
    hidden = await _picture(db_session, org_and_owner, name="hidden.png")
    await _run(db_session, qdrant, world, shown)
    await _run(db_session, qdrant, world, hidden)
    store = VectorStore(qdrant, model_id=EMBED_MODEL, vector_dim=DIM)

    async def search(**kw):
        return await store.search(
            query_vector=[1.0] + [0.0] * (DIM - 1),
            organization_id=org_and_owner.organization_id,
            **kw,
        )

    readable = [shown.space_id, hidden.space_id]
    assert len(await search(space_ids=readable)) == 2
    assert [
        h.document_id for h in await search(space_ids=readable, denied_document_ids=[hidden.id])
    ] == [shown.id]
    assert await search(space_ids=[]) == []
    assert await search(space_ids=[uuid.uuid4()]) == []
    got = await search(space_ids=readable)
    assert {h.source_kind for h in got} == {"image_caption"}


def test_a_replaced_file_forgets_the_old_description_state():
    from quberi_api.services.documents import new_version

    assert "image_caption" in new_version._FILE_BOUND_ATTRIBUTES


def test_the_registry_spec_is_not_assumed_for_a_name_the_administrator_typed():
    assert MODEL_REGISTRY.get("my-private-vl") is None
    assert isinstance(MODEL_REGISTRY[VISION_MODEL], ModelSpec)


# --------------------------------------------------------------------------
# Who gets described: the hook after a picture is indexed, and the catch-up
# --------------------------------------------------------------------------


async def test_descriptions_are_enabled_only_with_both_the_switch_and_a_model(
    db_session, org_and_owner, world
):
    org = org_and_owner.organization_id
    assert await image_caption.descriptions_enabled(db_session, org) is False  # switch off
    await _enable(db_session, org_and_owner)
    assert await image_caption.descriptions_enabled(db_session, org) is True
    world.chosen = False
    assert await image_caption.descriptions_enabled(db_session, org) is False  # no model


async def test_an_indexed_picture_is_queued_for_a_description_only_when_they_are_on(
    db_session, org_and_owner, world, monkeypatch
):
    from quberi_api.services.rag import indexer
    from quberi_api.workers import enqueue

    queued = []

    async def fake_enqueue(document_id, checksum):
        queued.append((document_id, checksum))

    monkeypatch.setattr(enqueue, "enqueue_image_caption", fake_enqueue)
    pic = await _picture(db_session, org_and_owner)
    doc = (await db_session.execute(select(Document).where(Document.id == pic.id))).scalar_one()

    await indexer._queue_picture_caption(db_session, doc)
    assert queued == []  # the switch is off: nothing is queued, so nothing can be sent

    await _enable(db_session, org_and_owner)
    await indexer._queue_picture_caption(db_session, doc)
    assert queued == [(pic.id, pic.checksum)]


async def test_a_failure_to_queue_never_undoes_the_pictures_own_index(
    db_session, org_and_owner, world, monkeypatch
):
    from quberi_api.services.rag import indexer
    from quberi_api.workers import enqueue

    async def boom(*_a):
        raise RuntimeError("redis is down")

    monkeypatch.setattr(enqueue, "enqueue_image_caption", boom)
    await _enable(db_session, org_and_owner)
    pic = await _picture(db_session, org_and_owner)
    doc = (await db_session.execute(select(Document).where(Document.id == pic.id))).scalar_one()

    await indexer._queue_picture_caption(db_session, doc)  # does not raise


async def _owed(db, limit=50):
    from quberi_api.services.rag import image_backfill

    return {i for i, _ in await image_backfill.pictures_needing_caption(db, limit=limit)}


async def test_the_catch_up_covers_only_organisations_that_turned_descriptions_on(
    db_session, org_and_owner
):
    pic = await _picture(db_session, org_and_owner)
    assert await _owed(db_session) == set()  # switch off

    await _enable(db_session, org_and_owner)
    assert await _owed(db_session) == {pic.id}


async def test_the_catch_up_leaves_out_what_must_not_be_sent_or_is_done(db_session, org_and_owner):
    await _enable(db_session, org_and_owner)
    space = await _space(db_session, org_and_owner)
    wanted = await _picture(db_session, org_and_owner, space, "wanted.png")
    done = await _picture(db_session, org_and_owner, space, "done.png")
    failed = await _picture(db_session, org_and_owner, space, "failed.png")
    replaced = await _picture(db_session, org_and_owner, space, "replaced.png")
    waiting = await _picture(
        db_session, org_and_owner, space, "waiting.png", review_state="pending"
    )
    guest = await _picture(
        db_session, org_and_owner, space, "guest.png", attributes={"guest_owner": "x"}
    )
    unindexed = await _picture(
        db_session, org_and_owner, space, "unindexed.png", indexing_state="not_indexed"
    )
    key = image_caption.IMAGE_CAPTION_KEY
    for pic, state in (
        (done, {"status": "indexed", "source_checksum": done.checksum}),
        (failed, {"status": "failed", "source_checksum": failed.checksum}),
        (replaced, {"status": "indexed", "source_checksum": "0" * 64}),
    ):
        await db_session.execute(
            update(Document).where(Document.id == pic.id).values(attributes={key: state})
        )
    await db_session.commit()

    owed = await _owed(db_session)

    assert owed == {wanted.id, replaced.id}
    assert not owed & {done.id, failed.id, waiting.id, guest.id, unindexed.id}


async def test_the_catch_up_does_not_cross_organisations(db_session, org_and_owner):
    from tests.test_external_save import _org

    other = await _org(db_session)
    await _enable(db_session, other)  # another organisation has them on
    mine = await _picture(db_session, org_and_owner)  # this one does not

    assert await _owed(db_session) == set()
    assert mine.id not in await _owed(db_session)


class _Redis:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, *args, _job_id=None):
        self.jobs.append((name, args, _job_id))
        return object()


def _ctx(db):
    class Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    return {"redis": _Redis(), "session_factory": Factory()}


async def test_the_worker_queues_a_small_batch_with_the_same_job_id_as_a_fresh_upload(
    db_session, org_and_owner, monkeypatch
):
    from quberi_api.workers import rag as worker

    monkeypatch.setattr(get_settings(), "vision_backfill_batch", 2)
    await _enable(db_session, org_and_owner)
    space = await _space(db_session, org_and_owner)
    pics = [await _picture(db_session, org_and_owner, space, f"p{i}.png") for i in range(3)]
    ctx = _ctx(db_session)

    assert await worker.backfill_picture_captions(ctx) == 2  # the batch size, not all three

    assert all(name == "index_image_caption_job" for name, _a, _j in ctx["redis"].jobs)
    queued = {a[0] for _n, a, _j in ctx["redis"].jobs}
    assert queued <= {str(p.id) for p in pics} and len(queued) == 2
    for _n, (doc_id,), job_id in ctx["redis"].jobs:
        pic = next(p for p in pics if str(p.id) == doc_id)
        assert job_id == f"image-caption-{doc_id}-{pic.checksum[:16]}"


@pytest.mark.parametrize("setting,value", [("vision_backfill_batch", 0), ("vision_daily_limit", 0)])
async def test_the_worker_does_nothing_when_the_catch_up_or_the_limit_is_zero(
    db_session, org_and_owner, monkeypatch, setting, value
):
    from quberi_api.workers import rag as worker

    monkeypatch.setattr(get_settings(), setting, value)
    await _enable(db_session, org_and_owner)
    await _picture(db_session, org_and_owner)
    ctx = _ctx(db_session)

    assert await worker.backfill_picture_captions(ctx) == 0
    assert ctx["redis"].jobs == []


def test_the_job_is_registered_and_the_catch_up_is_scheduled():
    from arq.cron import CronJob

    from quberi_api.workers import rag as worker

    assert image_caption.index_image_caption_job in worker.WorkerSettings.functions
    crons = {
        c.coroutine.__name__: c for c in worker.WorkerSettings.cron_jobs if isinstance(c, CronJob)
    }
    assert crons["backfill_picture_captions"].minute == 47


# --------------------------------------------------------------------------
# What a reader is told
# --------------------------------------------------------------------------


def test_chat_marks_a_description_as_made_by_a_model():
    from quberi_api.services.rag.generate import Citation, _where

    def cite(**kw):
        base = dict(
            index=1,
            document_id=uuid.uuid4(),
            space_id=uuid.uuid4(),
            document_name="a.png",
            chunk_index=1000,
            text="t",
            score=1.0,
            used=False,
            char_start=0,
            char_end=1,
        )
        base.update(kw)
        return Citation(**base)

    assert _where(cite(source_kind="image_caption")).endswith("（模型对图片的描述，可能有误）")
    assert "扫描识别" not in _where(cite(source_kind="image_caption"))
    assert _where(cite(source_kind="image_ocr")).endswith("（扫描识别，可能有误）")  # unchanged
    assert "描述" not in _where(cite())


def test_outside_tools_are_told_a_description_is_a_guide_not_a_transcript():
    from quberi_api.api.v1.external_schemas import Passage
    from quberi_api.services.mcp.formatting import _page_note

    def passage(**kw):
        base = dict(
            document_id=uuid.uuid4(),
            document_name="n",
            space_id=uuid.uuid4(),
            space_name="s",
            chunk_index=1000,
            text="t",
            score=1.0,
        )
        base.update(kw)
        return Passage(**base)

    assert _page_note(passage(source_kind="image_caption")) == (
        "described by a model from the picture, may contain errors"
    )
    assert _page_note(passage(source_kind="image_ocr")) == (
        "recognised from a scan, may contain errors"
    )
