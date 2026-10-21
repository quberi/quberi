"""The text inside a picture becomes searchable.

Real database, real indexer logic and in-memory Qdrant. The OCR engine and the embedding
model are stood in for (they have their own tests); what is pinned here is the wiring:
what gets filed, when it is read again and when it is not, and what it must not disturb.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from qdrant_client import AsyncQdrantClient
from sqlalchemy import update

from quberi_api.core.config import get_settings
from quberi_api.domain.documents.models import Document
from quberi_api.domain.spaces.models import Space
from quberi_api.services.ai.ocr import OcrImageError, OcrLine, OcrResult, OcrUnavailableError
from quberi_api.services.ai.ocr.models import ENGINE_NAME, ENGINE_VERSION, MODEL_NAME
from quberi_api.services.rag import image_text
from quberi_api.services.rag.image_text import (
    IMAGE_TEXT_KEY,
    build_block,
    index_image_text,
    normalise,
    unify_diameter,
)
from quberi_api.services.rag.indexer import IMAGE_INDEX_MODEL_KEY
from quberi_api.services.rag.vector_store import VectorStore
from tests.test_image_indexing import _doc, _png, _space

TEXT_MODEL = "test-embed"
TEXT_DIM = 8


# ---------------------------------------------------------------------------
# The block that gets embedded (pure)
# ---------------------------------------------------------------------------


def test_the_block_has_one_fixed_shape():
    block = build_block("flange.png", ["QS-FLG-STD", "法兰外径 520"])
    assert block == "文件名：flange.png\n图中文字：QS-FLG-STD；法兰外径 520"


def test_a_picture_with_no_text_is_still_findable_by_its_name():
    assert build_block("flange.png", []) == "文件名：flange.png"
    assert build_block("flange.png", ["", "   "]) == "文件名：flange.png"


@pytest.mark.parametrize(
    "raw, expected",
    [("ＱＳ－ＦＬＧ　１２０", "QS-FLG 120"), ("a   b\n c", "a b c"), ("Φ５２０", "Φ520")],
)
def test_normalise_folds_full_width_and_squeezes_whitespace(raw, expected):
    assert normalise(raw) == expected


@pytest.mark.parametrize("sign", list("Φϕ∅Øø⌀"))
def test_every_diameter_sign_unifies_to_one(sign):
    assert unify_diameter(f"{sign}520") == "Φ520"


def test_a_diameter_written_either_way_is_in_the_block_both_ways():
    block = build_block("x.png", ["Ø520H7"])
    assert "Ø520H7" in block and "Φ520H7" in block


def test_the_same_line_is_not_repeated():
    block = build_block("x.png", ["M12", "M12", "Φ20", "Φ20"])
    assert block.count("M12") == 1 and block.count("Φ20") == 1


def test_text_that_is_not_a_diameter_is_left_alone():
    assert unify_diameter("Q235B 去毛刺") == "Q235B 去毛刺"


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------


def _line(text, y=10, conf=0.95):
    return OcrLine(text, conf, ((10, y), (200, y), (200, y + 30), (10, y + 30)))


class FakeOcr:
    def __init__(self, lines=None, error=None):
        self.lines = lines if lines is not None else [_line("QS-FLG-STD"), _line("Φ520", y=60)]
        self.error = error
        self.calls = 0

    async def recognize(self, image):
        self.calls += 1
        if self.error:
            raise self.error
        return OcrResult(
            lines=list(self.lines), engine=ENGINE_NAME, model=MODEL_NAME, version=ENGINE_VERSION,
            width=640, height=480,
        )


class FakeEmbedder:
    vector_dim = TEXT_DIM

    def __init__(self):
        self.texts = []

    async def embed(self, texts, **_kw):
        self.texts.extend(texts)
        return [[1.0] + [0.0] * (TEXT_DIM - 1) for _ in texts]


@pytest.fixture
async def qdrant():
    client = AsyncQdrantClient(":memory:")
    yield client
    await client.close()


@pytest.fixture
def embedder(monkeypatch):
    instance = FakeEmbedder()
    state = {"model": TEXT_MODEL}

    async def _resolve(_session, *, organization_id):
        return SimpleNamespace(
            provider=instance,
            model=SimpleNamespace(model_id=state["model"], vector_dim=TEXT_DIM),
        )

    monkeypatch.setattr(image_text, "resolve_embedding_provider", _resolve)
    instance.use_model = lambda name: state.update(model=name)
    return instance


@pytest.fixture
def ocr(monkeypatch):
    engine = FakeOcr()
    monkeypatch.setattr(image_text, "get_ocr_provider", lambda: engine)
    return engine


@pytest.fixture
def blobs():
    store = {}

    async def load(storage_key):
        return store[storage_key]

    load.store = store
    return load


async def _picture(db, boot, blobs, name="flange.png", **kw):
    space = await _space(db, boot)
    doc = await _doc(db, boot, space, name, state="indexed", blobs=blobs.store, data=_png(), **kw)
    await db.execute(update(Document).where(Document.id == doc.id).values(review_state="approved"))
    await db.commit()
    return space, doc


def _text_store(qdrant, model=TEXT_MODEL):
    return VectorStore(qdrant, model_id=model, vector_dim=TEXT_DIM)


async def _run(db, qdrant, doc, blobs, **kw):
    return await index_image_text(
        db, qdrant=qdrant, document_id=doc.id, blob_loader=blobs, **kw
    )


async def _points(qdrant, model=TEXT_MODEL):
    if not await qdrant.collection_exists(_text_store(qdrant, model).collection):
        return []
    found, _ = await qdrant.scroll(
        _text_store(qdrant, model).collection, limit=100, with_payload=True
    )
    return [p.payload for p in found]


# ---------------------------------------------------------------------------
# Filing the text
# ---------------------------------------------------------------------------


async def test_the_text_is_filed_as_chunks_of_the_same_document(
    db_session, org_and_owner, qdrant, blobs, embedder, ocr
):
    space, doc = await _picture(db_session, org_and_owner, blobs)

    outcome = await _run(db_session, qdrant, doc, blobs)

    assert (outcome.status, outcome.ocr_ran, outcome.chunk_count) == ("indexed", True, 1)
    (payload,) = await _points(qdrant)
    assert payload["document_id"] == str(doc.id)
    assert payload["space_id"] == str(space.id)  # the same ids the permission filters use
    assert payload["organization_id"] == str(org_and_owner.organization_id)
    assert payload["source_kind"] == "image_ocr"
    assert "QS-FLG-STD" in payload["text"] and "Φ520" in payload["text"]
    assert payload["text"].startswith("文件名：flange.png")


async def test_the_text_is_found_by_a_search_that_uses_the_permission_filters(
    db_session, org_and_owner, qdrant, blobs, embedder, ocr
):
    space, doc = await _picture(db_session, org_and_owner, blobs)
    await _run(db_session, qdrant, doc, blobs)
    query = [1.0] + [0.0] * (TEXT_DIM - 1)

    hits = await _text_store(qdrant).search(
        query_vector=query, space_ids=[space.id], organization_id=org_and_owner.organization_id
    )
    assert [(h.document_id, h.source_kind) for h in hits] == [(doc.id, "image_ocr")]

    denied = await _text_store(qdrant).search(
        query_vector=query, space_ids=[space.id], organization_id=org_and_owner.organization_id,
        denied_document_ids=[doc.id],
    )
    assert denied == []  # a document-level deny hides the picture's text like any other
    elsewhere = await _text_store(qdrant).search(
        query_vector=query, space_ids=[uuid.uuid4()], organization_id=org_and_owner.organization_id
    )
    assert elsewhere == []


async def test_what_was_read_is_saved_on_the_document(
    db_session, org_and_owner, qdrant, blobs, embedder, ocr
):
    _, doc = await _picture(db_session, org_and_owner, blobs)
    await _run(db_session, qdrant, doc, blobs)
    await db_session.refresh(doc)

    state = doc.attributes[IMAGE_TEXT_KEY]
    assert state["status"] == "indexed"
    assert state["source_checksum"] == doc.checksum_sha256
    assert state["embedding_model"] == TEXT_MODEL
    assert state["ocr"]["version"] == ENGINE_VERSION
    first = state["ocr"]["lines"][0]
    assert first["text"] == "QS-FLG-STD" and first["conf"] == 0.95
    assert first["box"] == [[10.0, 10.0], [200.0, 10.0], [200.0, 40.0], [10.0, 40.0]]


async def test_it_leaves_the_other_attributes_alone(
    db_session, org_and_owner, qdrant, blobs, embedder, ocr
):
    _, doc = await _picture(db_session, org_and_owner, blobs)
    await db_session.execute(
        update(Document).where(Document.id == doc.id).values(
            attributes={IMAGE_INDEX_MODEL_KEY: "siglip2-base", "owner_note": "keep me"}
        )
    )
    await db_session.commit()

    await _run(db_session, qdrant, doc, blobs)
    await db_session.refresh(doc)

    assert doc.attributes[IMAGE_INDEX_MODEL_KEY] == "siglip2-base"
    assert doc.attributes["owner_note"] == "keep me"
    assert doc.attributes[IMAGE_TEXT_KEY]["status"] == "indexed"


async def test_it_does_not_overwrite_a_key_written_while_it_was_reading(
    db_session, org_and_owner, qdrant, blobs, embedder, ocr
):
    """The picture-embedding job can write its key during the seconds OCR takes."""
    _, doc = await _picture(db_session, org_and_owner, blobs)
    original = ocr.recognize

    async def recognize_then_someone_else_writes(image):
        result = await original(image)
        await db_session.execute(
            update(Document).where(Document.id == doc.id).values(
                attributes={IMAGE_INDEX_MODEL_KEY: "siglip2-base"}
            )
        )
        await db_session.commit()
        return result

    ocr.recognize = recognize_then_someone_else_writes
    await _run(db_session, qdrant, doc, blobs)
    await db_session.refresh(doc)
    assert doc.attributes[IMAGE_INDEX_MODEL_KEY] == "siglip2-base"
    assert IMAGE_TEXT_KEY in doc.attributes


# ---------------------------------------------------------------------------
# Not reading twice
# ---------------------------------------------------------------------------


async def test_a_picture_that_is_up_to_date_is_not_read_again(
    db_session, org_and_owner, qdrant, blobs, embedder, ocr
):
    _, doc = await _picture(db_session, org_and_owner, blobs)
    await _run(db_session, qdrant, doc, blobs)
    again = await _run(db_session, qdrant, doc, blobs)
    assert (again.status, again.reason) == ("skipped", "already up to date")
    assert ocr.calls == 1


async def test_force_reads_it_again(db_session, org_and_owner, qdrant, blobs, embedder, ocr):
    _, doc = await _picture(db_session, org_and_owner, blobs)
    await _run(db_session, qdrant, doc, blobs)
    await _run(db_session, qdrant, doc, blobs, force=True)
    assert ocr.calls == 2


async def test_a_replaced_picture_is_read_again_and_the_old_text_is_gone(
    db_session, org_and_owner, qdrant, blobs, embedder, ocr
):
    _, doc = await _picture(db_session, org_and_owner, blobs)
    await _run(db_session, qdrant, doc, blobs)
    ocr.lines = [_line("HZ-8800B")]
    await db_session.execute(
        update(Document).where(Document.id == doc.id).values(checksum_sha256="f" * 64)
    )
    await db_session.commit()

    outcome = await _run(db_session, qdrant, doc, blobs)

    assert outcome.ocr_ran and ocr.calls == 2
    (payload,) = await _points(qdrant)
    assert "HZ-8800B" in payload["text"] and "QS-FLG-STD" not in payload["text"]


async def test_a_new_embedding_model_re_embeds_the_saved_reading_without_reading_again(
    db_session, org_and_owner, qdrant, blobs, embedder, ocr
):
    _, doc = await _picture(db_session, org_and_owner, blobs)
    await _run(db_session, qdrant, doc, blobs)
    embedder.use_model("other-embed")

    outcome = await _run(db_session, qdrant, doc, blobs)

    assert (outcome.status, outcome.ocr_ran) == ("indexed", False)
    assert ocr.calls == 1  # the saved lines were reused
    (payload,) = await _points(qdrant, "other-embed")
    assert "QS-FLG-STD" in payload["text"]


async def test_a_new_ocr_engine_version_reads_again(
    db_session, org_and_owner, qdrant, blobs, embedder, ocr
):
    _, doc = await _picture(db_session, org_and_owner, blobs)
    await _run(db_session, qdrant, doc, blobs)
    state = {**doc.attributes[IMAGE_TEXT_KEY]}
    state["ocr"] = {**state["ocr"], "version": "older-engine"}
    await db_session.execute(
        update(Document).where(Document.id == doc.id).values(attributes={IMAGE_TEXT_KEY: state})
    )
    await db_session.commit()

    outcome = await _run(db_session, qdrant, doc, blobs)
    assert outcome.ocr_ran and ocr.calls == 2


async def test_indexing_twice_leaves_one_set_of_chunks(
    db_session, org_and_owner, qdrant, blobs, embedder, ocr
):
    _, doc = await _picture(db_session, org_and_owner, blobs)
    await _run(db_session, qdrant, doc, blobs, force=True)
    await _run(db_session, qdrant, doc, blobs, force=True)
    assert len(await _points(qdrant)) == 1


# ---------------------------------------------------------------------------
# When it must not run, and when it goes wrong
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change, reason",
    [
        ({"review_state": "pending"}, "not approved"),
        ({"status": "archived"}, "archived"),
        ({"storage_key": None}, "no stored file"),
    ],
)
async def test_it_does_not_read_what_it_should_not(
    db_session, org_and_owner, qdrant, blobs, embedder, ocr, change, reason
):
    _, doc = await _picture(db_session, org_and_owner, blobs)
    await db_session.execute(update(Document).where(Document.id == doc.id).values(**change))
    await db_session.commit()

    outcome = await _run(db_session, qdrant, doc, blobs)

    assert outcome.status == "skipped" and reason in outcome.reason
    assert ocr.calls == 0 and await _points(qdrant) == []


async def test_it_does_not_read_a_picture_in_an_inactive_space(
    db_session, org_and_owner, qdrant, blobs, embedder, ocr
):
    space, doc = await _picture(db_session, org_and_owner, blobs)
    await db_session.execute(update(Space).where(Space.id == space.id).values(status="archived"))
    await db_session.commit()
    outcome = await _run(db_session, qdrant, doc, blobs)
    assert outcome.status == "skipped" and "space" in outcome.reason
    assert ocr.calls == 0


async def test_it_does_nothing_when_ocr_is_switched_off(
    db_session, org_and_owner, qdrant, blobs, embedder, ocr, monkeypatch
):
    monkeypatch.setattr(get_settings(), "image_ocr_enabled", False)
    _, doc = await _picture(db_session, org_and_owner, blobs)
    outcome = await _run(db_session, qdrant, doc, blobs)
    assert outcome.status == "skipped" and "turned off" in outcome.reason
    assert ocr.calls == 0


async def test_a_missing_document_is_skipped_not_an_error(db_session, qdrant, blobs, embedder):
    outcome = await index_image_text(
        db_session, qdrant=qdrant, document_id=uuid.uuid4(), blob_loader=blobs
    )
    assert outcome.status == "skipped"


async def test_an_unreadable_picture_is_recorded_as_failed_and_stores_nothing(
    db_session, org_and_owner, qdrant, blobs, embedder, ocr
):
    ocr.error = OcrImageError("the file is not a readable picture")
    _, doc = await _picture(db_session, org_and_owner, blobs)

    outcome = await _run(db_session, qdrant, doc, blobs)

    assert outcome.status == "failed" and "not a readable picture" in outcome.reason
    await db_session.refresh(doc)
    assert doc.attributes[IMAGE_TEXT_KEY]["status"] == "failed"
    assert await _points(qdrant) == []


async def test_an_engine_that_cannot_run_leaves_no_state_so_it_is_tried_again(
    db_session, org_and_owner, qdrant, blobs, embedder, ocr
):
    ocr.error = OcrUnavailableError("the OCR engine was killed by the system")
    _, doc = await _picture(db_session, org_and_owner, blobs)

    outcome = await _run(db_session, qdrant, doc, blobs)

    assert outcome.status == "skipped"
    await db_session.refresh(doc)
    assert IMAGE_TEXT_KEY not in (doc.attributes or {})
    ocr.error = None
    assert (await _run(db_session, qdrant, doc, blobs)).status == "indexed"


async def test_a_failed_reading_keeps_the_text_that_was_already_filed(
    db_session, org_and_owner, qdrant, blobs, embedder, ocr
):
    _, doc = await _picture(db_session, org_and_owner, blobs)
    await _run(db_session, qdrant, doc, blobs)
    ocr.error = OcrUnavailableError("temporarily down")
    await _run(db_session, qdrant, doc, blobs, force=True)
    assert len(await _points(qdrant)) == 1  # not wiped by a transient failure


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_the_name_that_is_queued_is_a_function_the_worker_runs():
    """arq finds a job by function name; a mismatch fails silently, so pin it."""
    import inspect

    from quberi_api.workers import enqueue, rag

    registered = {fn.__name__ for fn in rag.WorkerSettings.functions}
    queued = inspect.getsource(enqueue.enqueue_image_text)
    assert "index_image_text_job" in registered
    assert '"index_image_text_job"' in queued


async def test_the_job_id_changes_when_the_picture_changes(monkeypatch):
    from quberi_api.workers import enqueue

    seen = []

    class Pool:
        async def enqueue_job(self, name, *args, _job_id=None):
            seen.append((name, args, _job_id))
            return SimpleNamespace(job_id=_job_id)

        async def close(self):
            return None

    async def fake_pool(_settings):
        return Pool()

    monkeypatch.setattr(enqueue, "create_pool", fake_pool)
    doc_id = uuid.uuid4()
    first = await enqueue.enqueue_image_text(doc_id, "a" * 64)
    second = await enqueue.enqueue_image_text(doc_id, "b" * 64)
    same = await enqueue.enqueue_image_text(doc_id, "a" * 64)
    assert first != second and first == same
    assert all(name == "index_image_text_job" for name, _, _ in seen)


async def test_a_picture_that_is_indexed_asks_for_its_text_to_be_read(
    db_session, org_and_owner, monkeypatch
):
    from quberi_api.services.rag import indexer
    from quberi_api.workers import enqueue

    queued = []

    async def fake_enqueue(document_id, checksum):
        queued.append((document_id, checksum))

    monkeypatch.setattr(enqueue, "enqueue_image_text", fake_enqueue)
    space = await _space(db_session, org_and_owner)
    doc = await _doc(db_session, org_and_owner, space)
    await indexer._queue_picture_text(doc)
    assert queued == [(doc.id, doc.checksum_sha256)]

    monkeypatch.setattr(get_settings(), "image_ocr_enabled", False)
    await indexer._queue_picture_text(doc)
    assert len(queued) == 1  # off means not even asked


async def test_failing_to_queue_never_undoes_the_picture(db_session, org_and_owner, monkeypatch):
    from quberi_api.services.rag import indexer
    from quberi_api.workers import enqueue

    async def broken(*_a, **_k):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(enqueue, "enqueue_image_text", broken)
    space = await _space(db_session, org_and_owner)
    doc = await _doc(db_session, org_and_owner, space)
    await indexer._queue_picture_text(doc)  # must not raise
