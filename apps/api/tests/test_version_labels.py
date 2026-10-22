"""A passage says which version of its document it was read from.

Retrieval labels each hit with the document's newest version; the chat prompt, the saved
history, held reviews, outside tools and the REST response carry it.
"""
# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)

from __future__ import annotations

import uuid

from quberi_api.services.rag.vector_store import RetrievedChunk
from tests.test_document_versions import (  # noqa: F401
    V1,
    V2,
    V3,
    _new_version,
    _upload,
    infra,
    world,
)

SPACE = uuid.uuid4()


# --------------------------------------------------------------------------
# Retrieval says which version is the newest
# --------------------------------------------------------------------------


def _hit(doc, version):
    return RetrievedChunk(
        document_id=doc, space_id=SPACE, document_name="leave.md", chunk_index=0, text="t",
        score=1.0, char_start=0, char_end=1, version_number=version,
    )


async def test_each_hit_is_labelled_with_the_newest_version_of_its_document(
    db_session, world, infra
):
    from quberi_api.services.rag.retrieve import _label_latest_versions

    versioned = await _upload(db_session, world, world.editor, "a.md", V1)
    await _new_version(db_session, world, world.editor, versioned, V2, name="a.md")
    single = await _upload(db_session, world, world.editor, "b.md", V3)
    external = uuid.uuid4()  # no version record at all
    await db_session.commit()

    labelled = await _label_latest_versions(
        db_session,
        [_hit(versioned.id, 1), _hit(versioned.id, 2), _hit(single.id, 1), _hit(external, 1)],
    )

    assert [(c.version_number, c.latest_version) for c in labelled] == [
        (1, 2), (2, 2), (1, 1), (1, None),
    ]
    assert await _label_latest_versions(db_session, []) == []


# --------------------------------------------------------------------------
# What a reader is told
# --------------------------------------------------------------------------


def _cite(**kw):
    from quberi_api.services.rag.generate import Citation

    base = dict(
        index=1, document_id=uuid.uuid4(), space_id=SPACE, document_name="leave.md",
        chunk_index=2, text="t", score=1.0, used=False, char_start=0, char_end=1,
    )
    base.update(kw)
    return Citation(**base)


def test_a_document_with_one_version_is_described_exactly_as_before():
    from quberi_api.services.rag.generate import _where

    assert _where(_cite()) == "chunk 2"
    assert _where(_cite(page_start=3, page_end=3, latest_version=1)) == "第3页"
    assert _where(_cite(page_start=3, version_number=1, latest_version=None)) == "第3页"


def test_a_passage_says_which_version_it_is_and_whether_it_was_replaced():
    from quberi_api.services.rag.generate import _where

    assert _where(_cite(page_start=3, page_end=3, version_number=2, latest_version=2)) == (
        "第3页，第2版（最新）"
    )
    assert _where(_cite(page_start=3, page_end=3, version_number=1, latest_version=2)) == (
        "第3页，第1版（旧版，已被第2版取代）"
    )
    assert _where(_cite(version_number=1, latest_version=3)) == (
        "chunk 2，第1版（旧版，已被第3版取代）"
    )


def test_the_prompt_asks_for_the_newest_version_only_when_an_older_one_is_quoted():
    from quberi_api.services.rag.generate import SUPERSEDED_RULE_ZH, _assemble_messages

    def system(citations):
        return _assemble_messages(query="q", citations=citations, history=[])[0].content

    assert SUPERSEDED_RULE_ZH not in system([_cite(version_number=2, latest_version=2)])
    assert SUPERSEDED_RULE_ZH not in system([_cite()])
    text = system([_cite(version_number=1, latest_version=2), _cite(index=2)])
    assert SUPERSEDED_RULE_ZH in text and "旧版，已被第2版取代" in text


def test_the_version_reaches_the_page_and_the_saved_history():
    from quberi_api.services.rag.generate import citation_to_dict

    d = citation_to_dict(_cite(version_number=1, latest_version=2))
    assert (d["version_number"], d["latest_version"]) == (1, 2)
    d = citation_to_dict(_cite())
    assert (d["version_number"], d["latest_version"]) == (1, None)


def test_a_held_review_keeps_the_version_and_an_old_one_reads_back_as_version_one():
    from quberi_api.services import send_review

    chunk = _hit(uuid.uuid4(), 1)
    chunk = RetrievedChunk(**{**chunk.__dict__, "latest_version": 3})
    back = send_review.chunk_from_dict(send_review.chunk_to_dict(chunk))
    assert (back.version_number, back.latest_version) == (1, 3)

    held = send_review.chunk_to_dict(chunk)
    held.pop("version_number")
    held.pop("latest_version")
    old = send_review.chunk_from_dict(held)
    assert (old.version_number, old.latest_version) == (1, None)


def test_outside_tools_get_the_version_in_the_fields_and_in_the_text():
    from quberi_api.api.v1.external_schemas import Passage
    from quberi_api.services.mcp.formatting import _page_note

    def passage(**kw):
        base = dict(
            document_id=uuid.uuid4(), document_name="n", space_id=SPACE, space_name="s",
            chunk_index=0, text="t", score=1.0,
        )
        base.update(kw)
        return Passage(**base)

    assert _page_note(passage()) == ""
    assert _page_note(passage(page_start=3, page_end=3, latest_version=1)) == "page 3"
    assert _page_note(passage(page_start=3, page_end=3, version_number=2, latest_version=2)) == (
        "page 3, version 2 (latest)"
    )
    assert _page_note(passage(version_number=1, latest_version=2)) == (
        "version 1 (older, replaced by version 2)"
    )
    scanned = passage(
        page_start=1, page_end=1, unit_label="ocr_block", version_number=1, latest_version=2
    )
    assert _page_note(scanned) == (
        "page 1 (recognised from a scan, may contain errors), "
        "version 1 (older, replaced by version 2)"
    )
    fields = passage(version_number=1, latest_version=2).model_dump()
    assert (fields["version_number"], fields["latest_version"]) == (1, 2)
