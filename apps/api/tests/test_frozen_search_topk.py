# ruff: noqa: E501
"""Freezing a space must not make a search return fewer results than asked for.

The frozen space is left out of the search scope before the vector search runs, so the
``limit`` is filled from spaces that can be read. Here the frozen space holds more points
than ``top_k`` and every one of them scores higher than anything readable: a filter applied
after the search would return nothing.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from qdrant_client import AsyncQdrantClient
from sqlalchemy import select, update

from quberi_api.domain.documents.models import Document
from quberi_api.domain.spaces.models import Space
from quberi_api.services.rag import retrieve
from quberi_api.services.rag.vector_store import ChunkPoint
from quberi_api.services.space_freeze import freeze_personal_spaces
from tests.test_document_acl import TEXT_DIM, TEXT_MODEL, _member, _text_store
from tests.test_external_save import _org
from tests.test_image_indexing import _doc
from tests.test_image_indexing import _space as _make_space
from tests.test_image_search import _grant

TOP_K = 3


async def personal_space_of(db, user_id):
    sid = (
        await db.execute(select(Space.id).where(Space.owner_id == user_id, Space.preset == "personal"))
    ).scalar_one()
    await db.commit()
    return sid


async def freeze_personal_space_of(db, boot, user_id):
    await db.commit()
    async with db.begin():
        await freeze_personal_spaces(
            db, organization_id=boot.organization_id, owner_user_id=user_id, actor_user_id=boot.user_id
        )


@pytest.fixture
async def qdrant():
    client = AsyncQdrantClient(":memory:")
    yield client
    await client.close()


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    class _Embedder:
        vector_dim = TEXT_DIM

        async def embed(self, texts, **_kw):
            return [[1.0] + [0.0] * (TEXT_DIM - 1) for _ in texts]

    async def _resolve(_session, *, organization_id):
        return SimpleNamespace(
            provider=_Embedder(), model=SimpleNamespace(model_id=TEXT_MODEL, vector_dim=TEXT_DIM)
        )

    monkeypatch.setattr(retrieve, "resolve_embedding_provider", _resolve)


async def _passage(db, boot, qdrant, space, name, *, close):
    """A document with one passage; ``close`` passages point the same way as the query."""
    doc = await _doc(db, boot, space, name, mime="application/pdf", state="indexed")
    vector = [1.0] + [0.0] * (TEXT_DIM - 1) if close else [0.6, 0.8] + [0.0] * (TEXT_DIM - 2)
    await _text_store(qdrant).upsert_document(
        organization_id=boot.organization_id,
        space_id=space.id,
        document_id=doc.id,
        document_name=name,
        document_checksum=None,
        chunks=[
            ChunkPoint(
                chunk_index=0, text=f"passage of {name}", vector=vector, char_start=0, char_end=10
            )
        ],
    )
    await db.execute(update(Document).where(Document.id == doc.id).values(indexing_state="indexed"))
    await db.commit()
    return doc


async def _scene(db, qdrant):
    boot = await _org(db)
    person = await _member(db, boot)
    personal = SimpleNamespace(id=await personal_space_of(db, person.id))
    shared_made = await _make_space(db, boot, "Shared")
    shared = SimpleNamespace(id=shared_made.id)
    await _grant(db, boot, shared, SimpleNamespace(id=person.id), "viewer")
    for n in range(TOP_K + 3):
        await _passage(db, boot, qdrant, personal, f"private-{n}.pdf", close=True)
    for n in range(TOP_K + 2):
        await _passage(db, boot, qdrant, shared, f"shared-{n}.pdf", close=False)
    return boot, person, personal, shared


async def test_a_frozen_space_with_better_scores_does_not_shrink_the_result(db_session, qdrant):
    boot, person, personal, shared = await _scene(db_session, qdrant)

    before = await retrieve.retrieve_for_user(
        db_session, qdrant=qdrant, organization_id=boot.organization_id,
        caller_user_id=person.id, query="passage", top_k=TOP_K,
    )
    assert len(before.chunks) == TOP_K
    assert {c.space_id for c in before.chunks} == {personal.id}  # the better scores win

    await freeze_personal_space_of(db_session, boot, person.id)

    after = await retrieve.retrieve_for_user(
        db_session, qdrant=qdrant, organization_id=boot.organization_id,
        caller_user_id=person.id, query="passage", top_k=TOP_K,
    )
    assert len(after.chunks) == TOP_K
    assert {c.space_id for c in after.chunks} == {shared.id}
    assert personal.id not in after.space_ids


async def test_the_search_is_only_asked_about_readable_spaces(db_session, qdrant, monkeypatch):
    boot, person, personal, shared = await _scene(db_session, qdrant)
    await freeze_personal_space_of(db_session, boot, person.id)
    asked: list[list] = []
    real = retrieve.VectorStore.search

    async def spy(self, *args, **kwargs):
        asked.append(list(kwargs.get("space_ids") or []))
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(retrieve.VectorStore, "search", spy)
    await retrieve.retrieve_for_user(
        db_session, qdrant=qdrant, organization_id=boot.organization_id,
        caller_user_id=person.id, query="passage", top_k=TOP_K,
    )
    assert asked, "the vector search did not run"
    for space_ids in asked:
        assert personal.id not in space_ids
        assert shared.id in space_ids
