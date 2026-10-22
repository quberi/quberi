"""Pictures indexed before their text could be read are caught up in the background.

Which pictures are picked, the worker job that queues them (a few per run, with the same job
id as a picture read right after upload), and that a picked picture ends up read and is not
picked again. The OCR engine and the embedding model are stood in for.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from qdrant_client import AsyncQdrantClient
from sqlalchemy import update

from quberi_api.core.config import get_settings
from quberi_api.domain.documents.models import Document
from quberi_api.domain.spaces.models import Space
from quberi_api.services.ai.ocr import OcrLine, OcrResult
from quberi_api.services.ai.ocr.models import ENGINE_NAME, ENGINE_VERSION, MODEL_NAME
from quberi_api.services.rag import image_backfill, image_text
from quberi_api.workers import rag as worker
from tests.test_image_indexing import _doc, _png, _space

OLD_ENGINE = "ocr-0"


def _state(checksum, status="indexed", version=ENGINE_VERSION):
    return {
        "status": status,
        "source_checksum": checksum,
        "embedding_model": "m",
        "ocr": {"version": version},
    }


async def _picture(db, boot, space, name, *, text_state="absent", **kw):
    """An indexed picture. ``text_state``: 'absent', 'current', 'old_engine', 'other_file',
    or 'failed'."""
    doc = await _doc(db, boot, space, name, state=kw.pop("state", "indexed"), **kw)
    attributes = {
        "absent": None,
        "current": {"image_text": _state(doc.checksum_sha256)},
        "old_engine": {"image_text": _state(doc.checksum_sha256, version=OLD_ENGINE)},
        "other_file": {"image_text": _state("0" * 64)},
        "failed": {"image_text": _state(doc.checksum_sha256, status="failed")},
        "failed_old_file": {"image_text": _state("0" * 64, status="failed")},
        "failed_old_engine": {
            "image_text": _state(doc.checksum_sha256, status="failed", version=OLD_ENGINE)
        },
    }[text_state]
    values = {"review_state": "approved"}
    if attributes is not None:
        values["attributes"] = attributes
    await db.execute(update(Document).where(Document.id == doc.id).values(**values))
    await db.commit()
    return SimpleNamespace(id=doc.id, checksum=doc.checksum_sha256)


async def test_the_pictures_that_were_never_read_or_read_by_something_else_are_picked(
    db_session, org_and_owner
):
    space = await _space(db_session, org_and_owner)
    never = await _picture(db_session, org_and_owner, space, "never.png")
    old_engine = await _picture(
        db_session, org_and_owner, space, "old.png", text_state="old_engine"
    )
    replaced = await _picture(
        db_session, org_and_owner, space, "swapped.png", text_state="other_file"
    )
    await _picture(db_session, org_and_owner, space, "done.png", text_state="current")
    await _picture(db_session, org_and_owner, space, "bad.png", text_state="failed")
    await _picture(db_session, org_and_owner, space, "bad2.png", text_state="failed_old_engine")
    replaced_bad = await _picture(
        db_session, org_and_owner, space, "bad-then-replaced.png", text_state="failed_old_file"
    )

    found = await image_backfill.pictures_needing_text(db_session, limit=50)

    # A failure is left alone while it is the same file; a different file is read afresh.
    assert {doc_id for doc_id, _ in found} == {
        never.id,
        old_engine.id,
        replaced.id,
        replaced_bad.id,
    }
    assert dict(found)[never.id] == never.checksum  # the checksum the job id is made from


async def test_what_should_not_be_read_is_left_out(db_session, org_and_owner):
    space = await _space(db_session, org_and_owner)
    gone_space = await _space(db_session, org_and_owner, "Archived")
    await _picture(db_session, org_and_owner, space, "waiting-for-index.png", state="not_indexed")
    archived = await _picture(db_session, org_and_owner, space, "archived.png")
    pending = await _picture(db_session, org_and_owner, space, "pending.png")
    no_file = await _picture(db_session, org_and_owner, space, "no-file.png")
    await _picture(db_session, org_and_owner, space, "report.pdf", mime="application/pdf")
    await _picture(db_session, org_and_owner, gone_space, "in-archived-space.png")
    await db_session.execute(
        update(Document).where(Document.id == archived.id).values(status="archived")
    )
    await db_session.execute(
        update(Document).where(Document.id == pending.id).values(review_state="pending")
    )
    await db_session.execute(
        update(Document).where(Document.id == no_file.id).values(storage_key=None)
    )
    await db_session.execute(
        update(Space).where(Space.id == gone_space.id).values(status="archived")
    )
    await db_session.commit()

    assert await image_backfill.pictures_needing_text(db_session, limit=50) == []


async def test_oldest_first_and_only_as_many_as_asked(db_session, org_and_owner):
    from datetime import timedelta

    space = await _space(db_session, org_and_owner)
    newest = await _picture(db_session, org_and_owner, space, "n.png", age=timedelta(minutes=1))
    oldest = await _picture(db_session, org_and_owner, space, "o.png", age=timedelta(days=2))
    middle = await _picture(db_session, org_and_owner, space, "m.png", age=timedelta(days=1))

    two = await image_backfill.pictures_needing_text(db_session, limit=2)
    assert [i for i, _ in two] == [oldest.id, middle.id]
    assert [i for i, _ in await image_backfill.pictures_needing_text(db_session, limit=9)] == [
        oldest.id,
        middle.id,
        newest.id,
    ]


# -- the worker ----------------------------------------------------------------------------


class FakeRedis:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, *args, _job_id=None):
        self.jobs.append((name, args, _job_id))
        return object()


def _ctx(db_session):
    class Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    return {"redis": FakeRedis(), "session_factory": Factory()}


async def test_the_worker_queues_a_batch_with_the_same_job_id_as_a_fresh_upload(
    db_session, org_and_owner, monkeypatch
):
    monkeypatch.setattr(get_settings(), "ocr_backfill_batch", 2)
    space = await _space(db_session, org_and_owner)
    from datetime import timedelta

    first = await _picture(db_session, org_and_owner, space, "a.png", age=timedelta(days=3))
    second = await _picture(db_session, org_and_owner, space, "b.png", age=timedelta(days=2))
    await _picture(db_session, org_and_owner, space, "c.png", age=timedelta(days=1))
    ctx = _ctx(db_session)

    assert await worker.backfill_picture_text(ctx) == 2  # the batch size, not everything

    assert ctx["redis"].jobs == [
        ("index_image_text_job", (str(first.id),), f"image-text-{first.id}-{first.checksum[:16]}"),
        (
            "index_image_text_job",
            (str(second.id),),
            f"image-text-{second.id}-{second.checksum[:16]}",
        ),
    ]


@pytest.mark.parametrize("setting,value", [("image_ocr_enabled", False), ("ocr_backfill_batch", 0)])
async def test_the_worker_does_nothing_when_reading_pictures_or_the_catch_up_is_off(
    db_session, org_and_owner, monkeypatch, setting, value
):
    monkeypatch.setattr(get_settings(), setting, value)
    space = await _space(db_session, org_and_owner)
    await _picture(db_session, org_and_owner, space, "a.png")
    ctx = _ctx(db_session)

    assert await worker.backfill_picture_text(ctx) == 0
    assert ctx["redis"].jobs == []


def test_the_catch_up_is_scheduled_hourly():
    from arq.cron import CronJob

    jobs = {
        c.coroutine.__name__: c for c in worker.WorkerSettings.cron_jobs if isinstance(c, CronJob)
    }
    assert (
        jobs["backfill_picture_text"].minute == 37 and jobs["backfill_picture_text"].run_at_startup
    )


# -- the whole way -------------------------------------------------------------------------


class Ocr:
    async def recognize(self, image):
        line = OcrLine("QS-FLG-STD", 0.95, ((0, 0), (100, 0), (100, 20), (0, 20)))
        return OcrResult(
            lines=[line],
            engine=ENGINE_NAME,
            model=MODEL_NAME,
            version=ENGINE_VERSION,
            width=100,
            height=100,
        )


class Embedder:
    vector_dim = 8

    async def embed(self, texts, **_kw):
        return [[1.0] + [0.0] * 7 for _ in texts]


async def test_a_picked_picture_is_read_filed_and_not_picked_again(
    db_session, org_and_owner, monkeypatch
):
    async def resolve(_session, *, organization_id):
        return SimpleNamespace(
            provider=Embedder(), model=SimpleNamespace(model_id="m", vector_dim=8)
        )

    monkeypatch.setattr(image_text, "resolve_embedding_provider", resolve)
    monkeypatch.setattr(image_text, "get_ocr_provider", lambda: Ocr())
    space = await _space(db_session, org_and_owner)
    pic = await _picture(db_session, org_and_owner, space, "plate.png")
    client = AsyncQdrantClient(":memory:")

    async def load(_key):
        return _png()

    try:
        (picked,) = await image_backfill.pictures_needing_text(db_session, limit=5)
        outcome = await image_text.index_image_text(
            db_session, qdrant=client, document_id=picked[0], blob_loader=load
        )
        assert outcome.status == "indexed" and outcome.chunk_count >= 1
    finally:
        await client.close()

    assert await image_backfill.pictures_needing_text(db_session, limit=5) == []
    assert pic.id == picked[0]
