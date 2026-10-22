"""Documents indexed before passages had a place are indexed again, a few an hour."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from qdrant_client import AsyncQdrantClient
from sqlalchemy import select, update

from quberi_api.core.config import get_settings
from quberi_api.domain.documents.models import Document
from quberi_api.domain.spaces.models import Space
from quberi_api.services.rag import image_backfill, indexer
from quberi_api.workers import rag as worker
from tests.test_image_indexing import _doc, _space

KEY = indexer.POSITIONS_KEY
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


async def _indexed(db, boot, space, name, *, mime="application/pdf", attributes=None, **values):
    age = values.pop("age", timedelta(0))
    doc = await _doc(db, boot, space, name, mime=mime, state="indexed", age=age)
    changes = {"review_state": "approved", **values}
    if attributes is not None:
        changes["attributes"] = attributes
    await db.execute(update(Document).where(Document.id == doc.id).values(changes))
    await db.commit()
    return doc.id


async def _owed(db, limit=50):
    return set(await image_backfill.documents_needing_positions(db, limit=limit))


async def test_a_document_indexed_before_places_existed_is_found(db_session, org_and_owner):
    space = await _space(db_session, org_and_owner)
    old = await _indexed(db_session, org_and_owner, space, "old.pdf")
    docx = await _indexed(db_session, org_and_owner, space, "old.docx", mime=DOCX)
    assert await _owed(db_session) == {old, docx}


async def test_a_document_that_has_its_places_is_left_alone(db_session, org_and_owner):
    space = await _space(db_session, org_and_owner)
    await _indexed(
        db_session, org_and_owner, space, "new.pdf", attributes={KEY: indexer.POSITIONS_FORMAT}
    )
    assert await _owed(db_session) == set()


async def test_a_newer_meaning_of_the_places_would_find_them_again(db_session, org_and_owner):
    space = await _space(db_session, org_and_owner)
    stale = await _indexed(db_session, org_and_owner, space, "a.pdf", attributes={KEY: 0})
    assert await _owed(db_session) == {stale}


async def test_what_must_not_be_touched_or_has_nothing_to_record_is_left_out(
    db_session, org_and_owner
):
    space = await _space(db_session, org_and_owner)
    gone_space = await _space(db_session, org_and_owner, "Archived")
    wanted = await _indexed(db_session, org_and_owner, space, "wanted.pdf")
    await _indexed(db_session, org_and_owner, space, "picture.png", mime="image/png")
    reference_only = await _indexed(db_session, org_and_owner, space, "feishu.docx")
    archived = await _indexed(db_session, org_and_owner, space, "archived.pdf")
    await _indexed(db_session, org_and_owner, space, "waiting.pdf", review_state="pending")
    failed = await _indexed(db_session, org_and_owner, space, "failed.pdf")
    queued = await _indexed(db_session, org_and_owner, space, "queued.pdf")
    await _indexed(db_session, org_and_owner, gone_space, "elsewhere.pdf")
    for doc_id, values in (
        (reference_only, {"storage_key": None}),
        (archived, {"status": "archived"}),
        (failed, {"indexing_state": "failed"}),
        (queued, {"indexing_state": "not_indexed"}),
    ):
        await db_session.execute(update(Document).where(Document.id == doc_id).values(values))
    await db_session.execute(
        update(Space).where(Space.id == gone_space.id).values(status="archived")
    )
    await db_session.commit()

    assert await _owed(db_session) == {wanted}


async def test_oldest_first_and_only_as_many_as_asked(db_session, org_and_owner):
    space = await _space(db_session, org_and_owner)
    newest = await _indexed(db_session, org_and_owner, space, "n.pdf", age=timedelta(minutes=1))
    oldest = await _indexed(db_session, org_and_owner, space, "o.pdf", age=timedelta(days=2))
    middle = await _indexed(db_session, org_and_owner, space, "m.pdf", age=timedelta(days=1))
    assert await image_backfill.documents_needing_positions(db_session, limit=2) == [
        oldest, middle,
    ]
    assert await image_backfill.documents_needing_positions(db_session, limit=9) == [
        oldest, middle, newest,
    ]


# -- the worker -----------------------------------------------------------------------------


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


async def test_the_worker_queues_a_small_batch_as_ordinary_indexing_jobs(
    db_session, org_and_owner, monkeypatch
):
    monkeypatch.setattr(get_settings(), "reindex_positions_batch", 2)
    space = await _space(db_session, org_and_owner)
    ids = [
        await _indexed(db_session, org_and_owner, space, f"{n}.pdf", age=timedelta(days=9 - n))
        for n in range(3)
    ]
    ctx = _ctx(db_session)

    assert await worker.backfill_positions(ctx) == 2  # the batch size, not all three

    assert ctx["redis"].jobs == [
        ("index_document", (str(i),), f"reindex-positions-{i}") for i in ids[:2]
    ]


async def test_the_worker_does_nothing_when_it_is_turned_off(
    db_session, org_and_owner, monkeypatch
):
    monkeypatch.setattr(get_settings(), "reindex_positions_batch", 0)
    space = await _space(db_session, org_and_owner)
    await _indexed(db_session, org_and_owner, space, "a.pdf")
    ctx = _ctx(db_session)
    assert await worker.backfill_positions(ctx) == 0
    assert ctx["redis"].jobs == []


def test_it_is_scheduled_hourly():
    from arq.cron import CronJob

    crons = {
        c.coroutine.__name__: c for c in worker.WorkerSettings.cron_jobs if isinstance(c, CronJob)
    }
    assert crons["backfill_positions"].minute == 17
    assert crons["backfill_positions"].run_at_startup


def test_the_default_batch_is_modest():
    assert get_settings().reindex_positions_batch == 20


# -- the whole way ---------------------------------------------------------------------------


class _Embedder:
    vector_dim = 8

    async def embed(self, texts, **_kw):
        return [[1.0] + [0.0] * 7 for _ in texts]


@pytest.fixture
def pipeline(monkeypatch):
    async def resolve(_session, *, organization_id):
        return SimpleNamespace(
            provider=_Embedder(), model=SimpleNamespace(model_id="m", vector_dim=8)
        )

    async def download(_key):
        return b"a paragraph long enough to be kept as a chunk of its own, with words.\n"

    monkeypatch.setattr(indexer, "resolve_embedding_provider", resolve)
    monkeypatch.setattr(indexer, "_download_blob", download)


async def test_indexing_again_records_the_places_and_the_document_is_not_picked_twice(
    db_session, org_and_owner, pipeline
):
    space = await _space(db_session, org_and_owner)
    doc_id = await _indexed(db_session, org_and_owner, space, "notes.txt", mime="text/plain")
    assert doc_id in await _owed(db_session)
    client = AsyncQdrantClient(":memory:")
    try:
        await indexer._run_pipeline(db_session, qdrant=client, document_id=doc_id)
    finally:
        await client.close()

    row = (await db_session.execute(select(Document).where(Document.id == doc_id))).scalar_one()
    assert row.attributes[KEY] == indexer.POSITIONS_FORMAT
    assert await _owed(db_session) == set()
