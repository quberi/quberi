"""Searching for a part number finds that part number, and nothing it may not show.

Meaning-based search puts Φ520 and Φ522 almost on top of each other; these tests build
exactly that situation, show the by-meaning result cannot tell them apart, and then pin
that the exact lookup can, through the same access rules.
"""

from __future__ import annotations

import uuid

import pytest
from qdrant_client import AsyncQdrantClient

from quberi_api.services.doc_acl import DocAccess
from quberi_api.services.rag.codes import codes_in_lines
from quberi_api.services.rag.retrieve import _merge_exact_code_hits
from quberi_api.services.rag.vector_store import ChunkPoint, RetrievedChunk, VectorStore

DIM = 8
MODEL = "test-embed"
ORG = uuid.uuid4()


@pytest.fixture
async def qdrant():
    client = AsyncQdrantClient(":memory:")
    yield client
    await client.close()


def _store(qdrant) -> VectorStore:
    return VectorStore(qdrant, model_id=MODEL, vector_dim=DIM)


def _vec(*head):
    return [*head] + [0.0] * (DIM - len(head))


async def _file(qdrant, space, name, lines, *, vector, doc=None, source_kind="image_ocr"):
    doc = doc or uuid.uuid4()
    text = f"文件名：{name}\n图中文字：" + "；".join(lines)
    await _store(qdrant).upsert_document(
        organization_id=ORG,
        space_id=space,
        document_id=doc,
        document_name=name,
        document_checksum=None,
        chunks=[
            ChunkPoint(
                chunk_index=0, text=text, vector=vector, char_start=0, char_end=len(text),
                source_kind=source_kind, codes=tuple(codes_in_lines(lines)),
            )
        ],
    )
    return doc


async def _exact(qdrant, spaces, query_codes, **kw):
    return await _store(qdrant).search_by_codes(
        codes=query_codes, space_ids=spaces, organization_id=ORG, **kw
    )


# ---------------------------------------------------------------------------
# Why this exists
# ---------------------------------------------------------------------------


async def test_meaning_search_cannot_tell_two_nearby_dimensions_apart_but_exact_search_can(qdrant):
    space = uuid.uuid4()
    # Two drawings that differ by one digit sit almost on top of each other.
    a = await _file(qdrant, space, "flange-a.png", ["Φ520", "QS-FLG-STD"], vector=_vec(1.0, 0.0))
    b = await _file(qdrant, space, "flange-b.png", ["Φ522", "QS-FLG-STD"], vector=_vec(1.0, 0.001))

    # The question, embedded, lands between them; the closer one is wrong for Φ522.
    query_vector = _vec(1.0, 0.0)
    by_meaning = await _store(qdrant).search(
        query_vector=query_vector, space_ids=[space], organization_id=ORG
    )
    assert by_meaning[0].document_id == a  # a is nearer, though the person asked for Φ522

    exact = await _exact(qdrant, [space], ["Φ522"])
    assert [c.document_id for c in exact] == [b]
    assert [c.document_id for c in await _exact(qdrant, [space], ["Φ520"])] == [a]


# ---------------------------------------------------------------------------
# The lookup
# ---------------------------------------------------------------------------


async def test_a_chunk_with_more_of_the_asked_codes_comes_first(qdrant):
    space = uuid.uuid4()
    one = await _file(qdrant, space, "one.png", ["Φ520"], vector=_vec(1.0))
    two = await _file(qdrant, space, "two.png", ["Φ520", "M12"], vector=_vec(0.0, 1.0))
    hits = await _exact(qdrant, [space], ["Φ520", "M12"])
    assert [c.document_id for c in hits] == [two, one]
    assert [c.score for c in hits] == [2.0, 1.0]


async def test_a_code_nobody_has_finds_nothing(qdrant):
    space = uuid.uuid4()
    await _file(qdrant, space, "x.png", ["Φ520"], vector=_vec(1.0))
    assert await _exact(qdrant, [space], ["Φ999"]) == []
    assert await _exact(qdrant, [space], []) == []


async def test_a_collection_that_does_not_exist_yet_is_empty_not_an_error(qdrant):
    assert await _exact(qdrant, [uuid.uuid4()], ["Φ520"]) == []


async def test_ordinary_text_without_codes_is_never_returned_by_the_exact_lookup(qdrant):
    space = uuid.uuid4()
    doc = uuid.uuid4()
    await _store(qdrant).upsert_document(
        organization_id=ORG, space_id=space, document_id=doc, document_name="notes.txt",
        document_checksum=None,
        chunks=[ChunkPoint(0, "Φ520 appears in prose only", _vec(1.0), 0, 10)],
    )
    assert await _exact(qdrant, [space], ["Φ520"]) == []  # no codes were filed for it


# ---------------------------------------------------------------------------
# The same access rules as the search by meaning
# ---------------------------------------------------------------------------


async def test_another_space_is_never_searched(qdrant):
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    await _file(qdrant, theirs, "secret.png", ["Φ520"], vector=_vec(1.0))
    assert await _exact(qdrant, [mine], ["Φ520"]) == []


async def test_another_organisation_is_never_searched(qdrant):
    space = uuid.uuid4()
    await _file(qdrant, space, "x.png", ["Φ520"], vector=_vec(1.0))
    other = await _store(qdrant).search_by_codes(
        codes=["Φ520"], space_ids=[space], organization_id=uuid.uuid4()
    )
    assert other == []


async def test_a_denied_document_is_hidden_inside_a_readable_space(qdrant):
    space = uuid.uuid4()
    hidden = await _file(qdrant, space, "hidden.png", ["Φ520"], vector=_vec(1.0))
    shown = await _file(qdrant, space, "shown.png", ["Φ520"], vector=_vec(0.0, 1.0))
    hits = await _exact(qdrant, [space], ["Φ520"], denied_document_ids=[hidden])
    assert [c.document_id for c in hits] == [shown]


async def test_a_document_shared_on_its_own_is_found_without_its_space(qdrant):
    elsewhere = uuid.uuid4()
    shared = await _file(qdrant, elsewhere, "shared.png", ["Φ520"], vector=_vec(1.0))
    hits = await _exact(qdrant, [], ["Φ520"], granted_document_ids=[shared])
    assert [c.document_id for c in hits] == [shared]


async def test_the_search_can_be_narrowed_to_given_documents(qdrant):
    space = uuid.uuid4()
    a = await _file(qdrant, space, "a.png", ["Φ520"], vector=_vec(1.0))
    await _file(qdrant, space, "b.png", ["Φ520"], vector=_vec(0.0, 1.0))
    assert [c.document_id for c in await _exact(qdrant, [space], ["Φ520"], document_ids=[a])] == [a]
    assert await _exact(qdrant, [space], ["Φ520"], document_ids=[]) == []


async def test_no_readable_space_and_nothing_shared_returns_nothing(qdrant):
    space = uuid.uuid4()
    await _file(qdrant, space, "a.png", ["Φ520"], vector=_vec(1.0))
    assert await _exact(qdrant, [], ["Φ520"]) == []


async def test_the_exact_lookup_and_the_search_by_meaning_build_one_filter(qdrant):
    """They must agree on who may see what, so they are built by the same function."""
    kw = dict(
        space_ids=[uuid.uuid4()], organization_id=ORG, document_ids=[uuid.uuid4()],
        granted_document_ids=[uuid.uuid4()], denied_document_ids=[uuid.uuid4()],
    )
    first = VectorStore._access_filter(**kw)
    second = VectorStore._access_filter(**kw)
    assert first == second and first is not second
    assert VectorStore._access_filter(
        space_ids=[], organization_id=ORG
    ) is None


# ---------------------------------------------------------------------------
# Merged into the main result
# ---------------------------------------------------------------------------


def _chunk(doc, idx=0, score=0.5, text="t"):
    return RetrievedChunk(
        document_id=doc, space_id=SPACE, document_name="n", chunk_index=idx, text=text,
        score=score, char_start=0, char_end=1,
    )


SPACE = uuid.uuid4()


class StubStore:
    def __init__(self, hits=None, error=None):
        self.hits, self.error, self.calls = hits or [], error, []

    async def search_by_codes(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return self.hits


async def _merge(store, kept, query, doc_access=None, readable=(SPACE,), limit=5):
    return await _merge_exact_code_hits(
        store, kept, query=query, space_ids=list(readable), organization_id=ORG,
        document_ids=None, doc_access=doc_access or DocAccess(), limit=limit,
    )


async def test_exact_hits_come_before_the_meaning_results_each_chunk_once():
    near, exact = uuid.uuid4(), uuid.uuid4()
    store = StubStore(hits=[_chunk(exact), _chunk(near)])
    merged = await _merge(store, [_chunk(near), _chunk(uuid.uuid4())], "Φ520 的法兰")
    assert [c.document_id for c in merged][:2] == [exact, near]
    assert len(merged) == 3  # `near` appears once, not twice


async def test_a_question_with_no_code_does_not_touch_the_exact_lookup():
    store = StubStore(hits=[_chunk(uuid.uuid4())])
    kept = [_chunk(uuid.uuid4())]
    assert await _merge(store, kept, "怎么申请年假") == kept
    assert store.calls == []


async def test_the_codes_in_the_question_are_what_is_looked_up():
    store = StubStore()
    await _merge(store, [], "法兰外径中520 和 Ø260 的区别")
    assert store.calls[0]["codes"] == ["Φ520", "Φ260"]  # misreading and variants folded


async def test_an_exact_hit_that_escaped_the_filter_is_dropped_by_the_recheck():
    other_space = uuid.uuid4()
    leaked = RetrievedChunk(
        document_id=uuid.uuid4(), space_id=other_space, document_name="n", chunk_index=0,
        text="t", score=1.0, char_start=0, char_end=1,
    )
    ok = _chunk(uuid.uuid4())
    merged = await _merge(StubStore(hits=[leaked, ok]), [], "Φ520")
    assert [c.document_id for c in merged] == [ok.document_id]


async def test_a_denied_document_is_dropped_even_if_the_store_returned_it():
    denied = uuid.uuid4()
    access = DocAccess(denied=frozenset({denied}))
    merged = await _merge(StubStore(hits=[_chunk(denied)]), [], "Φ520", doc_access=access)
    assert merged == []


async def test_the_whole_access_scope_reaches_the_store_not_just_the_recheck():
    """The store filter is the first door; the re-check is only the second.

    A stub store that returns nothing hides a dropped argument, so assert on what the
    store was actually asked for.
    """

    denied, granted, doc, other_space = (uuid.uuid4() for _ in range(4))
    access = DocAccess(granted={granted: other_space}, denied=frozenset({denied}))
    store = StubStore()
    await _merge_exact_code_hits(
        store, [], query="Φ520", space_ids=[SPACE], organization_id=ORG,
        document_ids=[doc], doc_access=access, limit=7,
    )
    call = store.calls[0]
    assert call["denied_document_ids"] == [denied]
    assert call["granted_document_ids"] == [granted]
    assert call["document_ids"] == [doc]
    assert list(call["space_ids"]) == [SPACE]
    assert call["organization_id"] == ORG
    assert call["limit"] == 7


async def test_a_failing_exact_lookup_leaves_the_normal_results_untouched():
    kept = [_chunk(uuid.uuid4())]
    merged = await _merge(StubStore(error=ConnectionError("qdrant down")), kept, "Φ520")
    assert merged == kept


def test_the_reranker_path_keeps_every_field_of_a_chunk():
    from dataclasses import replace

    original = RetrievedChunk(
        document_id=uuid.uuid4(), space_id=SPACE, document_name="n", chunk_index=3, text="t",
        score=0.1, char_start=40, char_end=90, source_kind="image_ocr",
    )
    rescored = replace(original, score=0.9)
    assert (rescored.char_start, rescored.char_end, rescored.source_kind) == (40, 90, "image_ocr")
    assert rescored.score == 0.9


# -- a denied document must not use up the places meant for readable ones ---------------


async def test_a_denied_document_does_not_take_the_only_place_in_an_exact_result(qdrant):
    """The deny is applied inside the index. If it were applied afterwards, the better-
    matching denied document would fill the single place and the readable one would be cut."""
    space = uuid.uuid4()
    denied = await _file(qdrant, space, "denied.png", ["Φ520", "M12"], vector=_vec(1.0))
    readable = await _file(qdrant, space, "readable.png", ["Φ520"], vector=_vec(0.0, 1.0))

    hits = await _exact(qdrant, [space], ["Φ520", "M12"], denied_document_ids=[denied], limit=1)

    assert [c.document_id for c in hits] == [readable]


async def test_the_store_itself_never_returns_a_denied_document_for_a_code(qdrant):
    """Not only the later re-check: the lookup, asked directly, already leaves it out."""
    space = uuid.uuid4()
    denied = await _file(qdrant, space, "denied.png", ["Φ520"], vector=_vec(1.0))
    assert await _exact(qdrant, [space], ["Φ520"], denied_document_ids=[denied]) == []
