# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""Versions end to end: the indexer files each version under its number, retrieval labels
the hits, and the web API adds, lists and downloads versions.

Real: database, services, indexer, retrieval, the vector store (in memory), the web routes.
Stood in: the embedding model, the file store and the job queue.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from qdrant_client import AsyncQdrantClient
from sqlalchemy import select

from quberi_api.api import dependencies
from quberi_api.api.errors import register_exception_handlers
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.api.v1.spaces import documents as documents_router
from quberi_api.core.config import get_settings
from quberi_api.domain.documents.models import Document
from quberi_api.domain.identity.models import Organization, User
from quberi_api.services.documents import core as documents_core
from quberi_api.services.documents import new_version
from quberi_api.services.rag import indexer, retrieve
from quberi_api.services.rag.vector_store import VectorStore
from tests.test_document_versions import (  # noqa: F401
    V1,
    V2,
    V3,
    _new_version,
    _row,
    _upload,
    infra,
    world,
)

TEXT_MODEL = "test-embed"
DIM = 8


class Embedder:
    vector_dim = DIM
    hook = None

    async def embed(self, texts, **_kw):
        if self.hook is not None:
            hook, self.hook = self.hook, None
            await hook()
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]


@pytest.fixture
def embedder(monkeypatch):
    emb = Embedder()

    async def _resolve(_session, *, organization_id):
        return SimpleNamespace(
            provider=emb, model=SimpleNamespace(model_id=TEXT_MODEL, vector_dim=DIM)
        )

    monkeypatch.setattr(indexer, "resolve_embedding_provider", _resolve)
    monkeypatch.setattr(retrieve, "resolve_embedding_provider", _resolve)
    return emb


@pytest.fixture
async def qdrant():
    client = AsyncQdrantClient(":memory:")
    yield client
    await client.close()


@pytest.fixture
def blobs(monkeypatch, infra):
    """Keep the bytes the (stood-in) file store was given, and serve them to the indexer."""
    kept: dict[str, bytes] = {}
    inner = documents_core.local_connector.store

    async def recording(*, storage_key, source_stream, content_type, max_bytes):
        kept[storage_key] = source_stream.read()
        source_stream.seek(0)
        return await inner(
            storage_key=storage_key, source_stream=source_stream,
            content_type=content_type, max_bytes=max_bytes,
        )

    async def download(storage_key: str) -> bytes:
        return kept[storage_key]

    async def open_stream(storage_key: str):
        yield kept[storage_key]

    monkeypatch.setattr(documents_core.local_connector, "store", recording)
    monkeypatch.setattr(indexer, "_download_blob", download)
    monkeypatch.setattr(new_version.local_connector, "open_stream", open_stream)
    return kept


async def _points(qdrant):
    store = VectorStore(qdrant, model_id=TEXT_MODEL, vector_dim=DIM)
    if not await qdrant.collection_exists(store.collection):
        return []
    found, _ = await qdrant.scroll(store.collection, limit=200, with_payload=True)
    return sorted(
        ((p.payload["version_number"], p.payload["text"]) for p in found), key=lambda t: t[0]
    )


async def _approved(db, doc_id):
    await db.commit()
    return (await db.execute(select(Document).where(Document.id == doc_id))).scalar_one()


# --------------------------------------------------------------------------
# The indexer
# --------------------------------------------------------------------------


async def test_each_version_is_indexed_under_its_own_number_and_the_older_stays(
    db_session, world, infra, blobs, embedder, qdrant
):
    doc = await _upload(db_session, world, world.editor, data=V1)
    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)
    assert [v for v, _ in await _points(qdrant)] == [1]

    await _new_version(db_session, world, world.editor, doc, V2)
    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)

    points = await _points(qdrant)
    assert [v for v, _ in points] == [1, 2]
    assert "5 days" in points[0][1] and "10 days" in points[1][1]
    row = await _approved(db_session, doc.id)
    assert (row.indexing_state, row.chunk_count) == ("indexed", 1)  # describes the current file


async def test_re_indexing_the_current_version_leaves_the_older_one_alone(
    db_session, world, infra, blobs, embedder, qdrant
):
    doc = await _upload(db_session, world, world.editor, data=V1)
    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)
    await _new_version(db_session, world, world.editor, doc, V2)
    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)

    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)  # "reindex"

    assert [v for v, _ in await _points(qdrant)] == [1, 2]


async def test_a_job_that_finishes_after_a_newer_version_arrived_does_not_claim_the_document(
    db_session, world, infra, blobs, embedder, qdrant
):
    doc = await _upload(db_session, world, world.editor, data=V1)

    async def a_new_version_arrives_while_the_old_one_is_being_read():
        await _new_version(db_session, world, world.editor, doc, V2)

    embedder.hook = a_new_version_arrives_while_the_old_one_is_being_read
    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)

    # What was read is filed as version 1, correctly; the document is not marked indexed
    # because the file it shows now (version 2) has not been read yet.
    assert [v for v, _ in await _points(qdrant)] == [1]
    row = await _approved(db_session, doc.id)
    assert (row.indexing_state, row.chunk_count) == ("not_indexed", 0)

    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)
    assert [v for v, _ in await _points(qdrant)] == [1, 2]
    assert (await _approved(db_session, doc.id)).indexing_state == "indexed"


async def test_retrieval_labels_every_hit_with_the_newest_version(
    db_session, world, infra, blobs, embedder, qdrant
):
    doc = await _upload(db_session, world, world.editor, data=V1)
    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)
    await _new_version(db_session, world, world.editor, doc, V2)
    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)
    await db_session.commit()

    result = await retrieve.retrieve_for_user(
        db_session, qdrant=qdrant, organization_id=world.boot.organization_id,
        caller_user_id=world.viewer.id, query="annual leave",
    )

    assert sorted((c.version_number, c.latest_version) for c in result.chunks) == [(1, 2), (2, 2)]


async def test_a_picture_text_job_files_its_text_under_the_current_version(
    db_session, world, infra, blobs, embedder, qdrant, monkeypatch
):
    """Text read out of the current picture replaces that version's text, not an older file's."""
    from quberi_api.services.ai.ocr import OcrLine, OcrResult
    from quberi_api.services.ai.ocr.models import ENGINE_NAME, ENGINE_VERSION, MODEL_NAME
    from quberi_api.services.rag import image_text

    class Ocr:
        async def recognize(self, image):
            line = OcrLine("QS-FLG-STD", 0.95, ((0, 0), (100, 0), (100, 20), (0, 20)))
            return OcrResult(
                lines=[line], engine=ENGINE_NAME, model=MODEL_NAME, version=ENGINE_VERSION,
                width=100, height=100,
            )

    monkeypatch.setattr(image_text, "get_ocr_provider", lambda: Ocr())
    monkeypatch.setattr(
        image_text, "resolve_embedding_provider", indexer.resolve_embedding_provider
    )
    doc = await _upload(db_session, world, world.editor, data=V1)
    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)
    await _new_version(db_session, world, world.editor, doc, b"png-ish bytes")

    async def load(key):
        return blobs[key]

    row = await _approved(db_session, doc.id)
    await image_text.index_image_text(
        db_session, qdrant=qdrant, document_id=doc.id, blob_loader=load
    )

    points = await _points(qdrant)
    assert [v for v, _ in points] == [1, 2]  # the text of version 1 is still there
    assert "QS-FLG-STD" in points[1][1] and row.current_version_id is not None


# --------------------------------------------------------------------------
# The web API
# --------------------------------------------------------------------------


@pytest.fixture
async def web(db_session):
    app = FastAPI()
    app.state.settings = get_settings()
    register_exception_handlers(app)
    app.include_router(documents_router.router, prefix="/api/v1")

    async def _session():
        yield db_session

    who = SimpleNamespace(user=None, organization=None)
    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[require_caller] = lambda: CallerContext(
        user=who.user, organization=who.organization, is_platform_admin=False
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.who = who  # type: ignore[attr-defined]
        yield client


async def _as(web, db, w, user):
    user_row = await db.get(User, user.id)
    org = await db.get(Organization, w.boot.organization_id)
    await db.commit()
    web.who.user, web.who.organization = user_row, org


def _file(data, name="leave.md"):
    return {"file": (name, io.BytesIO(data), "text/markdown")}


async def test_a_version_is_added_listed_and_downloaded_over_http(
    db_session, world, infra, blobs, web
):
    doc = await _upload(db_session, world, world.editor, data=V1)
    base = f"/api/v1/spaces/{world.space.id}/documents/{doc.id}"
    await _as(web, db_session, world, world.editor)

    added = await web.post(f"{base}/versions", files=_file(V2), data={"note": "10 days"})

    assert added.status_code == 201, added.text
    body = added.json()
    assert (body["version_number"], body["version_count"]) == (2, 2)
    assert body["checksum_sha256"] and body["indexing_state"] == "not_indexed"

    listed = (await web.get(f"{base}/versions")).json()
    assert [(v["version_number"], v["is_current"]) for v in listed["versions"]] == [
        (2, True), (1, False),
    ]
    assert listed["versions"][0]["note"] == "10 days"
    assert listed["versions"][0]["created_by_name"] == world.editor.display_name

    old = await web.get(f"{base}/versions/1/download")
    assert old.status_code == 200 and old.content == V1
    assert old.headers["x-document-version"] == "1"
    assert (await web.get(f"{base}/versions/2/download")).content == V2
    assert (await web.get(f"{base}/versions/7/download")).status_code == 404

    row = await web.get(base)
    assert (row.json()["version_number"], row.json()["version_count"]) == (2, 2)
    page = (await web.get(f"/api/v1/spaces/{world.space.id}/documents")).json()
    assert [(d["version_number"], d["version_count"]) for d in page["items"]] == [(2, 2)]


async def test_a_new_upload_reports_version_one(db_session, world, infra, blobs, web):
    await _as(web, db_session, world, world.editor)
    created = await web.post(f"/api/v1/spaces/{world.space.id}/documents", files=_file(V1))
    assert created.status_code == 201, created.text
    assert (created.json()["version_number"], created.json()["version_count"]) == (1, 1)


async def test_the_refusals_have_the_right_status(db_session, world, infra, blobs, web):
    doc = await _upload(db_session, world, world.editor, data=V1)
    other = await _upload(db_session, world, world.editor, "other.md", V3)
    base = f"/api/v1/spaces/{world.space.id}/documents/{doc.id}/versions"

    await _as(web, db_session, world, world.viewer)
    assert (await web.post(base, files=_file(V2))).status_code == 403

    await _as(web, db_session, world, world.editor)
    same = await web.post(base, files=_file(V1))
    assert same.status_code == 409 and "identical" in same.json()["detail"]
    dup = await web.post(base, files=_file(V3))
    assert dup.status_code == 409 and dup.json()["code"] == "duplicate_document"
    assert dup.json()["existing_document_id"] == str(other.id)

    await _as(web, db_session, world, world.stranger)
    assert (await web.post(base, files=_file(V2))).status_code == 404
    assert (await web.get(base)).status_code == 404

    await _as(web, db_session, world, world.editor)
    tail = await web.post(base.replace(str(doc.id), str(world.space.id)), files=_file(V2))
    assert tail.status_code == 404
    assert (await _row(db_session, doc.id)).checksum_sha256 is not None
