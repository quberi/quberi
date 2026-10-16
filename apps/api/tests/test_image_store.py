"""The picture vector store keeps pictures inside their organisation and space.

Runs against qdrant-client's in-memory engine, which evaluates the same filters
as the server, so these tests exercise the real isolation conditions.
"""

from __future__ import annotations

import uuid

import pytest
from qdrant_client import AsyncQdrantClient

from quberi_api.services.rag.image_store import IMAGE_COLLECTION_PREFIX, ImageVectorStore
from quberi_api.services.rag.vector_store import VectorStore, collection_name_for_model

DIM = 8


def _vec(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i % DIM] = 1.0
    return v


@pytest.fixture
async def qdrant():
    client = AsyncQdrantClient(":memory:")
    yield client
    await client.close()


def _store(qdrant) -> ImageVectorStore:
    return ImageVectorStore(qdrant, model_id="bge-vl-base", vector_dim=DIM)


async def _put(store, *, org, space, vec, name="a.png", doc=None):
    doc = doc or uuid.uuid4()
    await store.upsert_image(
        organization_id=org,
        space_id=space,
        document_id=doc,
        document_name=name,
        mime_type="image/png",
        document_checksum="abc",
        vector=vec,
    )
    return doc


def test_pictures_have_a_collection_of_their_own():
    assert IMAGE_COLLECTION_PREFIX == "quberi_images__"
    store = ImageVectorStore(None, model_id="bge-vl-base", vector_dim=DIM)  # type: ignore[arg-type]
    assert store.collection == "quberi_images__bge_vl_base"
    assert store.collection != collection_name_for_model("bge-vl-base")
    # The text store is unchanged.
    text_store = VectorStore(None, model_id="bge-m3", vector_dim=1024)  # type: ignore[arg-type]
    assert text_store.collection == "quberi_chunks__bge_m3"


async def test_the_nearest_picture_comes_first(qdrant):
    store, org, space = _store(qdrant), uuid.uuid4(), uuid.uuid4()
    red = await _put(store, org=org, space=space, vec=_vec(0), name="red.png")
    await _put(store, org=org, space=space, vec=_vec(3), name="other.png")
    hits = await store.search_images(query_vector=_vec(0), space_ids=[space], organization_id=org)
    assert hits[0].document_id == red
    assert hits[0].document_name == "red.png"
    assert hits[0].mime_type == "image/png"
    assert hits[0].space_id == space
    assert hits[0].score > hits[1].score


async def test_only_the_named_spaces_are_searched(qdrant):
    store, org = _store(qdrant), uuid.uuid4()
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    await _put(store, org=org, space=mine, vec=_vec(1), name="mine.png")
    await _put(store, org=org, space=theirs, vec=_vec(1), name="theirs.png")  # identical vector
    hits = await store.search_images(query_vector=_vec(1), space_ids=[mine], organization_id=org)
    assert [h.document_name for h in hits] == ["mine.png"]


async def test_another_organisation_is_never_searched_even_in_a_named_space(qdrant):
    store = _store(qdrant)
    org, other, space = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _put(store, org=org, space=space, vec=_vec(2), name="mine.png")
    await _put(store, org=other, space=space, vec=_vec(2), name="foreign.png")
    hits = await store.search_images(query_vector=_vec(2), space_ids=[space], organization_id=org)
    assert [h.document_name for h in hits] == ["mine.png"]


async def test_an_empty_space_set_finds_nothing_rather_than_everything(qdrant):
    store, org, space = _store(qdrant), uuid.uuid4(), uuid.uuid4()
    await _put(store, org=org, space=space, vec=_vec(0))
    assert await store.search_images(query_vector=_vec(0), space_ids=[], organization_id=org) == []


async def test_searching_before_any_picture_exists_is_empty(qdrant):
    hits = await _store(qdrant).search_images(
        query_vector=_vec(0), space_ids=[uuid.uuid4()], organization_id=uuid.uuid4()
    )
    assert hits == []


async def test_re_indexing_a_picture_replaces_its_vector(qdrant):
    store, org, space = _store(qdrant), uuid.uuid4(), uuid.uuid4()
    doc = await _put(store, org=org, space=space, vec=_vec(0), name="v1.png")
    await _put(store, org=org, space=space, vec=_vec(5), name="v2.png", doc=doc)
    assert await store.count_document(document_id=doc) == 1
    hits = await store.search_images(query_vector=_vec(5), space_ids=[space], organization_id=org)
    assert [h.document_name for h in hits] == ["v2.png"]


async def test_deleting_a_picture_removes_its_vector_and_only_its_own(qdrant):
    store, org, space = _store(qdrant), uuid.uuid4(), uuid.uuid4()
    gone = await _put(store, org=org, space=space, vec=_vec(0), name="gone.png")
    kept = await _put(store, org=org, space=space, vec=_vec(1), name="kept.png")
    await store.delete_document(document_id=gone)
    assert await store.count_document(document_id=gone) == 0
    assert await store.count_document(document_id=kept) == 1
    hits = await store.search_images(query_vector=_vec(0), space_ids=[space], organization_id=org)
    assert [h.document_name for h in hits] == ["kept.png"]


async def test_the_limit_is_honoured(qdrant):
    store, org, space = _store(qdrant), uuid.uuid4(), uuid.uuid4()
    for i in range(6):
        await _put(store, org=org, space=space, vec=_vec(i), name=f"{i}.png")
    hits = await store.search_images(
        query_vector=_vec(0), space_ids=[space], organization_id=org, limit=3
    )
    assert len(hits) == 3


async def test_an_empty_space_set_never_even_reaches_the_vector_database(qdrant):
    """The guard is explicit, not a side effect of how Qdrant treats an empty list."""

    store, org, space = _store(qdrant), uuid.uuid4(), uuid.uuid4()
    await _put(store, org=org, space=space, vec=_vec(0))
    calls = []

    class Spy:
        def __getattr__(self, name):
            if name == "query_points":
                async def spy(*args, **kwargs):
                    calls.append(kwargs)
                    return await qdrant.query_points(*args, **kwargs)
                return spy
            return getattr(qdrant, name)

    spied = ImageVectorStore(Spy(), model_id="bge-vl-base", vector_dim=DIM)  # type: ignore[arg-type]
    assert await spied.search_images(query_vector=_vec(0), space_ids=[], organization_id=org) == []
    assert calls == []
    found = await spied.search_images(query_vector=_vec(0), space_ids=[space], organization_id=org)
    assert len(found) == 1
    assert len(calls) == 1
