"""Text read out of a picture is hidden from whoever the picture is hidden from.

This drives the real path end to end: the real job reads the picture (the OCR engine is the
only stand-in), files its text, and the real retrieval used by chat and outside tools searches
it for different people, by meaning and by part number. The rule being pinned is that a picture
needs no permission code of its own for its text: it is filed under the picture's document and
space, and every existing rule follows.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import update

from quberi_api.domain.documents.models import Document
from quberi_api.services.ai.ocr import OcrLine, OcrResult
from quberi_api.services.ai.ocr.models import ENGINE_NAME, ENGINE_VERSION, MODEL_NAME
from quberi_api.services.rag import image_text, retrieve
from quberi_api.services.rag.image_text import index_image_text
from tests.test_document_acl import (  # noqa: F401  (fixtures are used by name)
    TEXT_DIM,
    TEXT_MODEL,
    _member,
    _rule,
    _unrule,
    fake_models,
    qdrant,
)
from tests.test_image_indexing import _doc, _png, _space
from tests.test_image_search import _grant


class ScriptedOcr:
    def __init__(self, lines):
        self.lines = lines

    async def recognize(self, image):
        return OcrResult(
            lines=self.lines, engine=ENGINE_NAME, model=MODEL_NAME, version=ENGINE_VERSION,
            width=100, height=100,
        )


def _line(text, y):
    return OcrLine(text, 0.95, ((0, y), (100, y), (100, y + 20), (0, y + 20)))


@pytest.fixture
async def world(db_session, org_and_owner, qdrant, monkeypatch):  # noqa: F811
    """A space with one picture whose text has been read, plus a member and an outsider."""
    boot = org_and_owner
    monkeypatch.setattr(
        image_text, "get_ocr_provider",
        lambda: ScriptedOcr([_line("QS-FLG-STD", 0), _line("Φ520", 40)]),
    )

    async def embed_resolve(_session, *, organization_id):
        class Embedder:
            vector_dim = TEXT_DIM

            async def embed(self, texts, **_kw):
                return [[1.0] + [0.0] * (TEXT_DIM - 1) for _ in texts]

        return SimpleNamespace(
            provider=Embedder(), model=SimpleNamespace(model_id=TEXT_MODEL, vector_dim=TEXT_DIM)
        )

    monkeypatch.setattr(image_text, "resolve_embedding_provider", embed_resolve)

    space = await _space(db_session, boot, "Parts")
    blobs = {}
    picture = await _doc(
        db_session, boot, space, "flange.png", state="indexed", blobs=blobs, data=_png()
    )
    await db_session.execute(
        update(Document).where(Document.id == picture.id).values(review_state="approved")
    )
    await db_session.commit()

    async def load(key):
        return blobs[key]

    outcome = await index_image_text(
        db_session, qdrant=qdrant, document_id=picture.id, blob_loader=load
    )
    assert outcome.status == "indexed"
    member = await _member(db_session, boot)
    outsider = await _member(db_session, boot)
    return SimpleNamespace(
        boot=boot, space=SimpleNamespace(id=space.id), picture=picture, member=member,
        outsider=outsider, qdrant=qdrant,
    )


async def _names(db, w, who, query, top_k=10):
    await db.commit()
    result = await retrieve.retrieve_for_user(
        db, qdrant=w.qdrant, organization_id=w.boot.organization_id, caller_user_id=who.id,
        query=query, top_k=top_k,
    )
    await db.commit()
    return sorted({c.document_name for c in result.chunks}), result.chunks


async def test_a_member_of_the_space_finds_the_pictures_text_by_meaning_and_by_part_number(
    db_session, world
):
    w, db = world, db_session
    await _grant(db, w.boot, w.space, w.member)

    by_meaning, _ = await _names(db, w, w.member, "法兰的图号")
    by_code, chunks = await _names(db, w, w.member, "Φ520 的法兰")

    assert by_meaning == ["flange.png"] and by_code == ["flange.png"]
    assert chunks[0].source_kind == "image_ocr"


async def test_someone_outside_the_space_finds_nothing_either_way(db_session, world):
    w, db = world, db_session
    assert (await _names(db, w, w.outsider, "法兰的图号"))[0] == []
    assert (await _names(db, w, w.outsider, "QS-FLG-STD"))[0] == []  # the exact lookup too


async def test_a_document_level_deny_hides_the_pictures_text_from_that_person_only(
    db_session, world
):
    w, db = world, db_session
    await _grant(db, w.boot, w.space, w.member)
    await _grant(db, w.boot, w.space, w.outsider)
    rule = await _rule(db, w.boot, w.space, w.picture, "deny", "user", w.member.id)

    for query in ("法兰的图号", "QS-FLG-STD", "Φ520"):
        assert (await _names(db, w, w.member, query))[0] == [], query
        assert (await _names(db, w, w.outsider, query))[0] == ["flange.png"], query

    await _unrule(db, w.boot, w.space, w.picture, rule)
    assert (await _names(db, w, w.member, "QS-FLG-STD"))[0] == ["flange.png"]  # back at once


async def test_the_exact_lookup_cannot_be_used_to_probe_for_a_hidden_part_number(
    db_session, world
):
    """Asking for a code that exists only in a document you may not read tells you nothing."""
    w, db = world, db_session
    await _grant(db, w.boot, w.space, w.member)
    await _rule(db, w.boot, w.space, w.picture, "deny", "user", w.member.id)
    names, chunks = await _names(db, w, w.member, "Φ520 QS-FLG-STD")
    assert names == [] and chunks == []


async def test_the_hidden_picture_does_not_take_a_result_slot_from_what_is_left(
    db_session, world, monkeypatch
):
    """A deny is applied inside the index, so the top_k results are filled with readable
    documents rather than being cut down afterwards."""
    from tests.test_document_acl import _text_doc

    w, db = world, db_session
    await _grant(db, w.boot, w.space, w.member)
    await _rule(db, w.boot, w.space, w.picture, "deny", "user", w.member.id)
    space = SimpleNamespace(id=w.space.id)
    for i in range(3):
        await _text_doc(db, w.boot, w.qdrant, space, f"readable-{i}.pdf")

    names, _ = await _names(db, w, w.member, "anything", top_k=3)

    assert names == ["readable-0.pdf", "readable-1.pdf", "readable-2.pdf"]
