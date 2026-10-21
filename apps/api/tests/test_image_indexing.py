"""Pictures are indexed by what they show, and old pictures are picked up.

The MinIO download and the image model are stood in for; the database, the
indexer, the picture vector store (qdrant-client's in-memory engine) and the
delete path are real.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image
from qdrant_client import AsyncQdrantClient
from sqlalchemy import update

from quberi_api.domain.documents.models import Document
from quberi_api.domain.spaces.models import Space
from quberi_api.services import document_sources as sources_service
from quberi_api.services import guest_sandbox
from quberi_api.services import spaces as spaces_service
from quberi_api.services.ai import local_image_embedding as lie
from quberi_api.services.ai.local_image_embedding import SIGLIP2_DIM, prepare_image
from quberi_api.services.rag import image_backfill, indexer
from quberi_api.services.rag.image_store import ImageVectorStore
from quberi_api.services.rag.vector_store import VectorStore

MODEL = "siglip2-base"


def _png(color="red", size=(24, 24)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class _FakeEmbedder:
    """Colour in, a one-hot vector out: red=0, green=1, blue=2 (the real decoder runs)."""

    model_id = MODEL
    vector_dim = SIGLIP2_DIM

    async def embed_images(self, images):
        rows = []
        for data in images:
            r, g, b = prepare_image(data).getpixel((0, 0))
            v = [0.0] * SIGLIP2_DIM
            v[int(max(range(3), key=lambda i: (r, g, b)[i]))] = 1.0
            rows.append(v)
        return rows


@pytest.fixture
def embedder(monkeypatch):
    instance = _FakeEmbedder()
    monkeypatch.setattr(indexer, "get_image_embedder", lambda *_a, **_k: instance)
    return instance


@pytest.fixture
async def qdrant():
    client = AsyncQdrantClient(":memory:")
    yield client
    await client.close()


@pytest.fixture
def blobs(monkeypatch):
    """What the 'download from MinIO' step returns, by storage key."""

    store: dict[str, bytes] = {}

    async def fake_download(storage_key: str) -> bytes:
        return store[storage_key]

    monkeypatch.setattr(indexer, "_download_blob", fake_download)
    return store


async def _space(db, boot, name="Parts"):
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


async def _doc(
    db,
    boot,
    space,
    name="part.png",
    *,
    mime="image/png",
    state="not_indexed",
    error=None,
    status="uploaded",
    blobs=None,
    data=None,
    age=timedelta(0),
):
    local = await sources_service.ensure_local_source(db, organization_id=boot.organization_id)
    await db.commit()
    key = f"{boot.organization_id}/{space.id}/{uuid.uuid4()}/{name}"
    doc = Document(
        organization_id=boot.organization_id,
        space_id=space.id,
        source_id=local.id,
        name=name,
        mime_type=mime,
        size_bytes=1,
        storage_key=key,
        checksum_sha256=uuid.uuid4().hex * 2,
        external_ref={},
        indexing_state=state,
        indexing_error=error,
        status=status,
    )
    db.add(doc)
    await db.flush()
    await db.commit()
    if blobs is not None:
        blobs[key] = data if data is not None else _png()
    if age:
        await db.execute(
            update(Document)
            .where(Document.id == doc.id)
            .values(created_at=datetime.now(timezone.utc) - age)
        )
        await db.commit()
    return doc


def _store(qdrant) -> ImageVectorStore:
    return ImageVectorStore(qdrant, model_id=MODEL, vector_dim=SIGLIP2_DIM)


# ---------------------------------------------------------------------------
# Indexing one picture
# ---------------------------------------------------------------------------


async def test_a_picture_is_indexed_into_the_picture_collection(
    db_session, org_and_owner, qdrant, blobs, embedder
):
    space = await _space(db_session, org_and_owner)
    doc = await _doc(db_session, org_and_owner, space, "gear.png", blobs=blobs, data=_png("blue"))

    result = await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)

    assert result.collection == "quberi_images__siglip2_base"
    assert result.chunk_count == 1
    await db_session.refresh(doc)
    assert (doc.indexing_state, doc.chunk_count, doc.indexing_error) == ("indexed", 1, None)
    assert doc.last_indexed_at is not None
    hits = await _store(qdrant).search_images(
        query_vector=[0.0, 0.0, 1.0] + [0.0] * (SIGLIP2_DIM - 3),
        space_ids=[space.id],
        organization_id=org_and_owner.organization_id,
    )
    assert [(h.document_id, h.document_name, h.space_id) for h in hits] == [
        (doc.id, "gear.png", space.id)
    ]


async def test_a_picture_never_touches_the_text_collection(
    db_session, org_and_owner, qdrant, blobs, embedder
):
    space = await _space(db_session, org_and_owner)
    doc = await _doc(db_session, org_and_owner, space, blobs=blobs)
    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)
    names = [c.name for c in (await qdrant.get_collections()).collections]
    assert names == ["quberi_images__siglip2_base"]


async def test_indexing_twice_leaves_one_vector(db_session, org_and_owner, qdrant, blobs, embedder):
    space = await _space(db_session, org_and_owner)
    doc = await _doc(db_session, org_and_owner, space, blobs=blobs)
    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)
    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)
    assert await _store(qdrant).count_document(document_id=doc.id) == 1


@pytest.mark.parametrize(
    "data, reason",
    [
        (b"this is not a picture", "not a readable picture"),
        (b"", "the image is empty"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "not a readable picture"),
    ],
)
async def test_a_bad_picture_fails_with_a_picture_reason_and_stores_nothing(
    db_session, org_and_owner, qdrant, blobs, embedder, data, reason
):
    space = await _space(db_session, org_and_owner)
    doc = await _doc(db_session, org_and_owner, space, blobs=blobs, data=data)

    with pytest.raises(lie.ImageDecodeError):
        await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)

    await db_session.refresh(doc)
    assert doc.indexing_state == "failed"
    assert doc.indexing_error.startswith("image: ")
    assert reason in doc.indexing_error
    assert await _store(qdrant).count_document(document_id=doc.id) == 0


async def test_an_unsupported_picture_format_is_refused_with_a_clear_reason(
    db_session, org_and_owner, qdrant, blobs, embedder
):
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, format="BMP")
    space = await _space(db_session, org_and_owner)
    doc = await _doc(
        db_session, org_and_owner, space, "scan.bmp", mime="image/bmp",
        blobs=blobs, data=buf.getvalue(),
    )
    with pytest.raises(lie.ImageDecodeError):
        await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)
    await db_session.refresh(doc)
    assert "only PNG, JPEG, GIF and WebP" in doc.indexing_error


async def test_picture_search_can_be_turned_off_and_says_so(
    db_session, org_and_owner, qdrant, blobs, embedder, monkeypatch
):
    from quberi_api.core.config import get_settings

    monkeypatch.setattr(get_settings(), "image_search_enabled", False)
    space = await _space(db_session, org_and_owner)
    doc = await _doc(db_session, org_and_owner, space, blobs=blobs)
    with pytest.raises(RuntimeError, match="disabled"):
        await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)
    await db_session.refresh(doc)
    assert doc.indexing_state == "failed"
    assert "turned off" in doc.indexing_error
    assert await _store(qdrant).count_document(document_id=doc.id) == 0


def test_it_is_on_by_default():
    from quberi_api.core.config import Settings

    assert Settings.model_fields["image_search_enabled"].default is True
    assert Settings.model_fields["default_image_embedding_model"].default == "siglip2-base"


@pytest.mark.parametrize(
    "mime, expected",
    [("image/png", True), ("IMAGE/JPEG", True), ("image/svg+xml", True), ("application/pdf", False),
     ("text/plain", False), ("", False), (None, False)],
)
def test_which_documents_count_as_pictures(mime, expected):
    assert indexer.is_picture(mime) is expected


# ---------------------------------------------------------------------------
# Pictures that were uploaded before this existed
# ---------------------------------------------------------------------------

OLD_ERROR = (
    "extract: no extractor for mime='image/png' filename='x.png' "
    "— OCR / images / scanned PDFs land in H-3"
)


async def test_old_pictures_are_found_for_backfill(db_session, org_and_owner):
    space = await _space(db_session, org_and_owner)
    queued = await _doc(db_session, org_and_owner, space, "queued.png", state="not_indexed")
    old = await _doc(db_session, org_and_owner, space, "old.jpg", mime="image/jpeg",
                     state="failed", error=OLD_ERROR)

    ids = await image_backfill.pictures_needing_index(db_session, model_id=MODEL)

    assert set(ids) == {queued.id, old.id}


async def test_backfill_leaves_everything_that_should_not_be_retried(db_session, org_and_owner):
    space = await _space(db_session, org_and_owner)
    gone_space = await _space(db_session, org_and_owner, "Archived")
    await db_session.execute(
        update(Space).where(Space.id == gone_space.id).values(status="archived")
    )
    await db_session.commit()
    done = await _doc(db_session, org_and_owner, space, "done.png", state="indexed")
    await _record_model(db_session, done, MODEL)
    await _doc(db_session, org_and_owner, space, "indexing.png", state="indexing")
    await _doc(db_session, org_and_owner, space, "corrupt.png", state="failed",
               error="image: the file is not a readable picture")
    await _doc(
        db_session, org_and_owner, space, "archived.png", state="not_indexed", status="archived"
    )
    await _doc(db_session, org_and_owner, space, "report.pdf", mime="application/pdf",
               state="failed", error="extract: pypdf returned no text")
    await _doc(db_session, org_and_owner, gone_space, "in-archived-space.png", state="not_indexed")

    assert await image_backfill.pictures_needing_index(db_session, model_id=MODEL) == []


async def test_backfill_takes_the_oldest_first_and_respects_the_limit(db_session, org_and_owner):
    space = await _space(db_session, org_and_owner)
    newest = await _doc(db_session, org_and_owner, space, "new.png", age=timedelta(minutes=1))
    oldest = await _doc(db_session, org_and_owner, space, "old.png", age=timedelta(days=3))
    middle = await _doc(db_session, org_and_owner, space, "mid.png", age=timedelta(days=1))

    two = await image_backfill.pictures_needing_index(db_session, model_id=MODEL, limit=2)
    assert two == [oldest.id, middle.id]
    assert await image_backfill.pictures_needing_index(db_session, model_id=MODEL, limit=10) == [
        oldest.id, middle.id, newest.id
    ]


async def test_a_backfilled_picture_ends_up_searchable(
    db_session, org_and_owner, qdrant, blobs, embedder
):
    space = await _space(db_session, org_and_owner)
    old = await _doc(db_session, org_and_owner, space, "old.png", state="failed", error=OLD_ERROR,
                     blobs=blobs, data=_png("green"))

    (picked,) = await image_backfill.pictures_needing_index(db_session, model_id=MODEL)
    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=picked)

    assert await image_backfill.pictures_needing_index(db_session, model_id=MODEL) == []  # not picked up again
    await db_session.refresh(old)
    assert old.indexing_state == "indexed"


async def _record_model(db, doc, model_id):
    """Say which picture model made this picture's vector (as indexing does)."""
    await db.execute(
        update(Document)
        .where(Document.id == doc.id)
        .values(attributes={indexer.IMAGE_INDEX_MODEL_KEY: model_id})
    )
    await db.commit()


async def test_a_picture_that_failed_for_a_reason_other_than_the_picture_is_retried(
    db_session, org_and_owner
):
    space = await _space(db_session, org_and_owner)
    transient = await _doc(db_session, org_and_owner, space, "later.png", state="failed",
                           error="RuntimeError: qdrant unreachable")
    await _doc(db_session, org_and_owner, space, "corrupt.png", state="failed",
               error="image: the file is not a readable picture")
    no_reason = await _doc(db_session, org_and_owner, space, "no-reason.png", state="failed", error=None)
    ids = await image_backfill.pictures_needing_index(db_session, model_id=MODEL)
    assert set(ids) == {transient.id, no_reason.id}


async def test_pictures_indexed_under_another_model_are_found_for_re_indexing(
    db_session, org_and_owner
):
    space = await _space(db_session, org_and_owner)
    current = await _doc(db_session, org_and_owner, space, "current.png", state="indexed")
    await _record_model(db_session, current, MODEL)
    old_model = await _doc(db_session, org_and_owner, space, "old-model.png", state="indexed")
    await _record_model(db_session, old_model, "bge-vl-base")
    # Indexed before models were recorded: made by the first model, bge-vl-base.
    legacy = await _doc(db_session, org_and_owner, space, "legacy.png", state="indexed")
    ids = await image_backfill.pictures_needing_index(db_session, model_id=MODEL)
    assert set(ids) == {old_model.id, legacy.id}
    # Pointed back at the old model, the pictures made by it no longer need work,
    # and the one made by the new model does.
    back = await image_backfill.pictures_needing_index(db_session, model_id="bge-vl-base")
    assert set(back) == {current.id}


async def test_re_indexing_moves_the_vector_to_the_new_model_and_drops_the_old_one(
    db_session, org_and_owner, qdrant, blobs, embedder
):
    space = await _space(db_session, org_and_owner)
    doc = await _doc(db_session, org_and_owner, space, "moved.png", state="indexed",
                     blobs=blobs, data=_png("blue"))
    old_store = ImageVectorStore(qdrant, model_id="bge-vl-base", vector_dim=512)
    await old_store.upsert_image(
        organization_id=org_and_owner.organization_id, space_id=space.id, document_id=doc.id,
        document_name=doc.name, mime_type="image/png", document_checksum=None,
        vector=[1.0] + [0.0] * 511,
    )
    assert await old_store.count_document(document_id=doc.id) == 1
    assert doc.id in await image_backfill.pictures_needing_index(db_session, model_id=MODEL)

    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)

    assert await _store(qdrant).count_document(document_id=doc.id) == 1
    assert await old_store.count_document(document_id=doc.id) == 0
    await db_session.refresh(doc)
    assert doc.attributes[indexer.IMAGE_INDEX_MODEL_KEY] == MODEL
    assert doc.indexing_state == "indexed"
    assert await image_backfill.pictures_needing_index(db_session, model_id=MODEL) == []


async def test_indexing_leaves_the_documents_other_attributes_alone(
    db_session, org_and_owner, qdrant, blobs, embedder
):
    space = await _space(db_session, org_and_owner)
    doc = await _doc(db_session, org_and_owner, space, "keep.png", blobs=blobs)
    await db_session.execute(
        update(Document).where(Document.id == doc.id).values(attributes={"owner_note": "keep me"})
    )
    await db_session.commit()
    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)
    await db_session.refresh(doc)
    assert doc.attributes == {"owner_note": "keep me", indexer.IMAGE_INDEX_MODEL_KEY: MODEL}


async def test_deleting_a_picture_removes_its_vector_under_every_model(db_session, qdrant):
    doc = uuid.uuid4()
    stores = [
        ImageVectorStore(qdrant, model_id=m, vector_dim=d) for m, d in lie.IMAGE_MODEL_DIMS.items()
    ]
    assert len(stores) == 2
    for store, dim in zip(stores, lie.IMAGE_MODEL_DIMS.values()):
        await store.upsert_image(
            organization_id=uuid.uuid4(), space_id=uuid.uuid4(), document_id=doc,
            document_name="x.png", mime_type="image/png", document_checksum=None,
            vector=[1.0] + [0.0] * (dim - 1),
        )
    await indexer.remove_image_from_index(qdrant=qdrant, document_id=doc)
    assert [await s.count_document(document_id=doc) for s in stores] == [0, 0]


async def test_deleting_a_picture_that_has_no_vector_anywhere_is_quiet(qdrant):
    await indexer.remove_image_from_index(qdrant=qdrant, document_id=uuid.uuid4())  # no collections


async def test_the_worker_queues_each_picture_once_and_does_nothing_when_off(
    db_session, org_and_owner, monkeypatch
):
    from quberi_api.core.config import get_settings
    from quberi_api.workers import rag as worker

    space = await _space(db_session, org_and_owner)
    one = await _doc(db_session, org_and_owner, space, "a.png")
    two = await _doc(db_session, org_and_owner, space, "b.png")

    class FakeRedis:
        def __init__(self):
            self.jobs = []

        async def enqueue_job(self, name, *args, _job_id=None):
            self.jobs.append((name, args, _job_id))
            return object()

    class Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    redis = FakeRedis()
    ctx = {"redis": redis, "session_factory": Factory()}
    assert await worker.backfill_pictures(ctx) == 2
    assert {(n, a, j) for n, a, j in redis.jobs} == {
        ("index_document", (str(one.id),), f"index-picture-{one.id}"),
        ("index_document", (str(two.id),), f"index-picture-{two.id}"),
    }

    monkeypatch.setattr(get_settings(), "image_search_enabled", False)
    redis.jobs.clear()
    assert await worker.backfill_pictures(ctx) == 0
    assert redis.jobs == []


# ---------------------------------------------------------------------------
# Deleting
# ---------------------------------------------------------------------------


async def test_deleting_a_picture_removes_its_vector(
    db_session, org_and_owner, qdrant, blobs, embedder, monkeypatch
):
    from quberi_api.infrastructure import qdrant as qdrant_module
    from quberi_api.services.documents import core as documents_core

    space = await _space(db_session, org_and_owner)
    doc = await _doc(db_session, org_and_owner, space, blobs=blobs)
    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)
    assert await _store(qdrant).count_document(document_id=doc.id) == 1

    class Shared:
        """The in-memory client, whose close() must not end the test's client."""

        def __getattr__(self, name):
            return getattr(qdrant, name)

        async def close(self):
            return None

    async def no_blob_delete(_key):
        return None

    monkeypatch.setattr(qdrant_module, "create_qdrant_client", lambda _settings: Shared())
    monkeypatch.setattr(documents_core.local_connector, "delete", no_blob_delete)

    await documents_core.delete_document(
        db_session,
        document_id=doc.id,
        organization_id=org_and_owner.organization_id,
        space_id=space.id,
        caller_user_id=org_and_owner.user_id,
    )

    assert await _store(qdrant).count_document(document_id=doc.id) == 0


async def test_the_verified_sandbox_purge_covers_pictures_too(qdrant):
    org, space, doc = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    pic = _store(qdrant)
    await pic.upsert_image(
        organization_id=org, space_id=space, document_id=doc, document_name="x.png",
        mime_type="image/png", document_checksum=None, vector=[1.0] + [0.0] * (SIGLIP2_DIM - 1),
    )
    text = VectorStore(qdrant, model_id="bge-m3", vector_dim=4)

    without = await guest_sandbox.purge_vectors(
        qdrant=qdrant, document_id=doc, model_id="bge-m3", vector_dim=4
    )
    assert without.vectors_left == 0  # the text store alone cannot see the picture
    assert await pic.count_document(document_id=doc) == 1  # ... and it is still there

    with_pictures = await guest_sandbox.purge_vectors(
        qdrant=qdrant, document_id=doc, model_id="bge-m3", vector_dim=4, image_model_id=MODEL
    )
    assert with_pictures.vectors_left == 0
    assert await pic.count_document(document_id=doc) == 0
    assert text is not None


async def test_the_purge_does_not_claim_clean_when_a_picture_vector_will_not_go(qdrant):
    doc = uuid.uuid4()

    class Stubborn(ImageVectorStore):
        async def delete_document(self, *, document_id):
            return None  # the delete silently does nothing

        async def count_document(self, *, document_id):
            return 1

    import quberi_api.services.rag.image_store as image_store_module

    original = image_store_module.ImageVectorStore
    image_store_module.ImageVectorStore = Stubborn
    try:
        result = await guest_sandbox.purge_vectors(
            qdrant=qdrant, document_id=doc, model_id="bge-m3", vector_dim=4,
            image_model_id=MODEL, attempts=2,
        )
    finally:
        image_store_module.ImageVectorStore = original
    # One stubborn vector under each picture model this build knows.
    assert result.vectors_left == len(lie.IMAGE_MODEL_DIMS)


async def test_the_purge_also_clears_a_vector_left_under_the_previous_picture_model(qdrant):
    org, space, doc = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    old = ImageVectorStore(qdrant, model_id="bge-vl-base", vector_dim=512)
    await old.upsert_image(
        organization_id=org, space_id=space, document_id=doc, document_name="x.png",
        mime_type="image/png", document_checksum=None, vector=[1.0] + [0.0] * 511,
    )
    result = await guest_sandbox.purge_vectors(
        qdrant=qdrant, document_id=doc, model_id="bge-m3", vector_dim=4, image_model_id=MODEL
    )
    assert result.vectors_left == 0
    assert await old.count_document(document_id=doc) == 0


async def test_the_sandbox_cleanup_asks_for_pictures_to_be_purged_as_well(
    db_session, org_and_owner, monkeypatch
):
    from types import SimpleNamespace

    from quberi_api.services import sandbox_cleanup

    seen = {}

    async def fake_purge(**kwargs):
        seen.update(kwargs)
        return guest_sandbox.PurgeResult(document_id=kwargs["document_id"], vectors_left=0)

    async def fake_resolve(_session, *, organization_id):
        return SimpleNamespace(model=SimpleNamespace(model_id="bge-m3", vector_dim=1024))

    class FakeQdrant:
        async def close(self):
            return None

    async def fake_delete(*_a, **_k):
        return None

    monkeypatch.setattr(sandbox_cleanup, "resolve_embedding_provider", fake_resolve)
    monkeypatch.setattr(sandbox_cleanup, "create_qdrant_client", lambda _s: FakeQdrant())
    monkeypatch.setattr(sandbox_cleanup.guest_sandbox, "purge_vectors", fake_purge)
    monkeypatch.setattr(sandbox_cleanup.documents_core, "delete_document", fake_delete)

    doc_id = uuid.uuid4()
    outcome = await sandbox_cleanup.delete_sandbox_document(
        db_session,
        document_id=doc_id,
        space_id=uuid.uuid4(),
        org_id=org_and_owner.organization_id,
        actor_user_id=org_and_owner.user_id,
    )

    assert outcome.row_deleted and outcome.vectors_verified_gone
    assert seen["image_model_id"] == "siglip2-base"
    assert seen["model_id"] == "bge-m3"
