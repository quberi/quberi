# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""A picture found by what it shows points at the passage found in its text.

The same file can come back twice for one question: as a possible-match picture (with a
preview) and as a passage read out of it. Neither is dropped; the picture says which passage
is the same file. Only ids of results that were already allowed are compared.
"""

from __future__ import annotations

import uuid

from quberi_api.api.v1.external_schemas import (
    ImageResult,
    Passage,
    SearchImageResult,
    SearchResponse,
)
from quberi_api.services.mcp import formatting
from quberi_api.services.rag.picture_links import link_chat_pictures, passage_numbers_by_document
from quberi_api.services.rag.vector_store import RetrievedChunk
from tests.test_external_image_search import (  # noqa: F401
    _org,
    _stored,
    blobs,
    fake_embedder,
    http,
    qdrant,
    redis_client,
    signing_key,
)
from tests.test_external_search_kind import _ask, text_side  # noqa: F401
from tests.test_image_indexing import _space as _make_space
from tests.test_image_search import BLUE, RED
from tests.test_send_review import FakeProvider, model  # noqa: F401  (fixture)

# -- the helpers ---------------------------------------------------------------------------


def test_passages_are_numbered_from_one_in_the_order_given():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert passage_numbers_by_document([a, b, a]) == {a: [1, 3], b: [2]}
    assert passage_numbers_by_document([]) == {}


def test_a_chat_picture_gets_the_index_of_its_first_citation_only_when_it_has_one():
    doc, other = str(uuid.uuid4()), str(uuid.uuid4())
    pictures = [{"document_id": doc, "document_name": "a.png"}, {"document_id": other}]
    citations = [
        {"document_id": str(uuid.uuid4()), "index": 1},
        {"document_id": doc, "index": 2},
        {"document_id": doc, "index": 3},
    ]

    linked = link_chat_pictures(pictures, citations)

    assert linked[0]["citation_index"] == 2
    assert "citation_index" not in linked[1]
    assert "citation_index" not in pictures[0]  # the input is left as it was
    assert link_chat_pictures(pictures, []) == pictures
    assert link_chat_pictures([], citations) == []


def test_malformed_citations_are_ignored_not_trusted():
    doc = str(uuid.uuid4())
    junk = [{"document_id": doc}, {"index": 4}, {"document_id": 7, "index": 1},
            {"document_id": doc, "index": "2"}]
    assert link_chat_pictures([{"document_id": doc}], junk) == [{"document_id": doc}]


# -- outside tools -------------------------------------------------------------------------


def _chunk(doc_id, space_id, name, source_kind="text"):
    return RetrievedChunk(
        document_id=doc_id, space_id=space_id, document_name=name, chunk_index=0, text="t",
        score=0.5, char_start=0, char_end=1, source_kind=source_kind,
    )


async def test_a_picture_found_by_sight_and_by_its_text_says_which_passage_is_the_same_file(
    http, db_session, redis_client, qdrant, blobs, text_side
):
    boot = await _org(db_session)
    space = await _make_space(db_session, boot, "Animals")
    lioness = await _stored(db_session, boot, qdrant, space, BLUE, blobs, "lioness.png")
    other = await _stored(db_session, boot, qdrant, space, RED, blobs, "notes.png")
    text_side.spaces = [space.id]
    text_side.chunks = [
        _chunk(other.id, space.id, "notes.png", "image_ocr"),
        _chunk(lioness.id, space.id, "lioness.png", "image_ocr"),
    ]

    body = (await _ask(http, db_session, redis_client, boot, {})).json()

    assert [p["document_name"] for p in body["passages"]] == ["notes.png", "lioness.png"]
    (image,) = body["images"]  # neither result is dropped
    assert image["name"] == "lioness.png" and image["preview_url"]
    assert image["passage_numbers"] == [2]


async def test_a_picture_with_no_passage_of_its_own_has_no_pointer(
    http, db_session, redis_client, qdrant, blobs, text_side
):
    boot = await _org(db_session)
    space = await _make_space(db_session, boot, "Animals")
    await _stored(db_session, boot, qdrant, space, BLUE, blobs, "lioness.png")
    doc = await _stored(db_session, boot, qdrant, space, RED, blobs, "x.png")
    text_side.spaces = [space.id]
    text_side.chunks = [_chunk(doc.id, space.id, "notes.md")]

    body = (await _ask(http, db_session, redis_client, boot, {})).json()

    assert body["images"][0]["passage_numbers"] == []


def _result(images, passages=()):
    return SearchResponse(
        organization="Acme", passages=list(passages), searched_spaces=1, images=images,
        preview_expires_in=600,
    )


def _image(name, numbers=()):
    return SearchImageResult(
        name=name, space_name="Animals", mime_type="image/png", size_bytes=1, score=0.5,
        preview_url=f"https://x.test/{name}", passage_numbers=list(numbers),
    )


def _passage(name):
    return Passage(
        document_id=uuid.uuid4(), document_name=name, space_id=uuid.uuid4(), space_name="s",
        chunk_index=0, text="body", score=0.5, source_kind="image_ocr",
    )


def test_the_text_a_model_reads_points_the_picture_at_its_passage():
    text = formatting.format_search_result(
        "q", _result([_image("a.png", [2]), _image("b.png")], [_passage("x"), _passage("a.png")])
    )

    assert "Its text also matched this question: see passage [2] above." in text
    assert text.count("Its text also matched") == 1  # only the picture that has a passage


def test_a_plain_picture_list_is_unchanged():
    text = formatting.format_search_result("q", _result([_image("a.png")], [_passage("x")]))
    assert "Its text also matched" not in text


def test_the_dedicated_picture_search_is_unchanged():
    from quberi_api.api.v1.external_schemas import ImageSearchResponse

    text = formatting.format_image_search_result(
        "q",
        ImageSearchResponse(
            organization="Acme", images=[_image("a.png")], searched_spaces=1,
            preview_expires_in=600,
        ),
    )
    assert "Its text also matched" not in text


def test_the_added_field_is_optional_for_old_data():
    old = ImageResult(name="a", space_name="s", mime_type="image/png", size_bytes=1, score=0.1)
    assert "passage_numbers" not in old.model_dump()  # the picture search is unchanged
    assert SearchImageResult(**old.model_dump()).passage_numbers == []

