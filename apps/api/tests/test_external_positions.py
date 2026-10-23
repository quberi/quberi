"""Search results for outside tools (MCP, REST, agent tools) say which page they are on.

Outside tools depend on the response shape, so the rule here is: fields are only added, never
changed, and a passage with no recorded page reads exactly as it did before.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from quberi_api.api.v1.external_schemas import Passage, SearchResponse
from quberi_api.services.mcp.formatting import _page_note, format_search_result
from quberi_api.services.rag.vector_store import RetrievedChunk


def _passage(**kw) -> Passage:
    base = dict(
        document_id=uuid.uuid4(), document_name="contract.pdf", space_id=uuid.uuid4(),
        space_name="Legal", chunk_index=3, text="The supplier shall deliver.", score=0.8123,
    )
    base.update(kw)
    return Passage(**base)


def _response(*passages) -> SearchResponse:
    return SearchResponse(
        organization="Acme", searched_spaces=1, passages=list(passages), pending_documents=[],
        pending_total=0, images=[], preview_expires_in=0,
    )


def _result_line(*passages) -> str:
    text = format_search_result("delivery", _response(*passages))
    return next(ln for ln in text.splitlines() if ln.startswith("[1]"))


# -- the response shape: additions only -------------------------------------------------


def test_the_original_seven_fields_are_unchanged_and_first():
    fields = list(Passage.model_fields)
    assert fields[:7] == [
        "document_id", "document_name", "space_id", "space_name", "chunk_index", "text", "score",
    ]
    assert fields[7:] == [
        "page_start", "page_end", "unit_label", "source_kind", "version_number", "latest_version",
        "section", "unit_start", "unit_end", "char_start", "char_end", "page_preview_url",
        "citation",
    ]


def test_the_added_fields_are_optional_so_old_callers_and_old_data_still_validate():
    old = dict(
        document_id=str(uuid.uuid4()), document_name="a.md", space_id=str(uuid.uuid4()),
        space_name="s", chunk_index=0, text="t", score=0.5,
    )
    passage = Passage(**old)
    assert (passage.page_start, passage.page_end, passage.unit_label) == (None, None, "")
    assert passage.source_kind == "text"


def test_the_json_a_client_receives_carries_the_page():
    body = _passage(page_start=3, page_end=4, unit_label="ocr_block").model_dump(mode="json")
    assert body["page_start"] == 3 and body["page_end"] == 4 and body["unit_label"] == "ocr_block"
    assert body["chunk_index"] == 3  # still there for callers that use it


# -- the text a model reads -------------------------------------------------------------


def test_a_passage_with_no_page_reads_exactly_as_before():
    assert _result_line(_passage()) == "[1] contract.pdf (space: Legal), relevance 0.81"


def test_one_page_is_named():
    line = _result_line(_passage(page_start=3, page_end=3))
    assert line == "[1] contract.pdf (space: Legal), page 3, relevance 0.81"


def test_a_page_range_is_named():
    assert "pages 2-4," in _result_line(_passage(page_start=2, page_end=4))


def test_text_recognised_from_a_scan_is_marked_so_the_model_can_say_it():
    line = _result_line(_passage(page_start=3, page_end=3, unit_label="ocr_block"))
    assert "page 3 (recognised from a scan, may contain errors)," in line


def test_text_read_out_of_a_picture_is_marked_even_without_a_page():
    line = _result_line(_passage(source_kind="image_ocr"))
    assert line == (
        "[1] contract.pdf (space: Legal), recognised from a scan, may contain errors, "
        "relevance 0.81"
    )


def test_the_files_own_text_is_never_marked_as_recognised():
    assert "scan" not in _result_line(_passage(page_start=1, page_end=1, unit_label="text_block"))


def test_a_passage_without_a_space_name_still_gets_its_page():
    line = _result_line(_passage(space_name="", page_start=9, page_end=9))
    assert line == "[1] contract.pdf, page 9, relevance 0.81"


def test_the_instructions_tell_the_model_to_give_the_page_and_to_flag_scans():
    text = format_search_result("q", _response(_passage()))
    assert "Give the page too when one is shown" in text
    assert "recognised from a scan may contain misread characters" in text


@pytest.mark.parametrize(
    "kw, expected",
    [
        ({}, ""),
        ({"page_start": 1, "page_end": 1}, "page 1"),
        ({"page_start": 1, "page_end": None}, "page 1"),
        ({"page_start": 1, "page_end": 5}, "pages 1-5"),
        ({"unit_label": "ocr_block"}, "recognised from a scan, may contain errors"),
    ],
)
def test_the_page_note(kw, expected):
    assert _page_note(_passage(**kw)) == expected


# -- the adapter copies the fields from a real retrieved chunk ---------------------------


def test_every_position_field_of_a_retrieved_chunk_reaches_the_outside_response():
    """A field added to RetrievedChunk and not to Passage would never leave the server."""
    wanted = {"page_start", "page_end", "unit_label", "source_kind"}
    assert wanted <= set(RetrievedChunk.__dataclass_fields__)
    assert wanted <= set(Passage.model_fields)


async def test_the_adapter_fills_the_passage_from_the_chunk(monkeypatch):
    import inspect

    from quberi_api.services.mcp import adapters

    source = inspect.getsource(adapters)
    for name in ("page_start=c.page_start", "page_end=c.page_end", "unit_label=c.unit_label",
                 "source_kind=c.source_kind"):
        assert name in source, name


# -- the agent tool ---------------------------------------------------------------------


async def test_the_agent_search_tool_returns_the_page(monkeypatch):
    from quberi_api.services.agents import tools

    chunk = RetrievedChunk(
        document_id=uuid.uuid4(), space_id=uuid.uuid4(), document_name="a.pdf", chunk_index=1,
        text="t", score=0.5, char_start=0, char_end=1, page_start=6, page_end=7,
        unit_label="ocr_block", source_kind="text",
    )

    async def fake(*_a, **_k):
        return SimpleNamespace(chunks=[chunk], space_ids=[uuid.uuid4()])

    monkeypatch.setattr(tools, "retrieve_with_scope", fake)
    ctx = SimpleNamespace(
        session=None, organization_id=uuid.uuid4(), user_id=uuid.uuid4(),
        knowledge_scope_mode="user", knowledge_space_ids=[], require_qdrant=lambda: None,
    )
    out = await tools.SearchKnowledgeTool().run(ctx, {"query": "q"})
    passage = out["passages"][0]
    assert (passage["page_start"], passage["page_end"]) == (6, 7)
    assert passage["unit_label"] == "ocr_block" and passage["source_kind"] == "text"
    assert passage["chunk_index"] == 1  # unchanged
