"""PDF extraction keeps where each piece of text came from.

The PDFs are built by hand (``tests/pdf_fixtures.py``), so the page a sentence is on and
the gaps between paragraphs are known exactly.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from quberi_api.services.rag import extract
from quberi_api.services.rag.extract import ExtractError, extract_text
from tests.pdf_fixtures import blank_pdf, scanned_pdf, text_pdf

PDF = "application/pdf"


def _extract(blob):
    return extract_text(blob=blob, filename="doc.pdf", mime_type=PDF)


def _png(color="white"):
    buf = io.BytesIO()
    Image.new("RGB", (120, 160), color).save(buf, format="PNG")
    return buf.getvalue()


CONTRACT = text_pdf(
    [
        [
            (72, 72, 16, "Contract Title"),
            (72, 110, 11, "1. The supplier shall deliver"),
            (72, 124, 11, "the goods within 30 days."),
            (72, 160, 11, "2. Payment is due on receipt"),
            (72, 174, 11, "of the invoice."),
        ],
        [
            (72, 72, 11, "Page two begins here."),
            (72, 86, 11, "Second line of the same paragraph."),
            (72, 200, 11, "A separate paragraph far below."),
        ],
    ]
)


# -- positions ---------------------------------------------------------------------------


def test_every_block_knows_its_page():
    result = _extract(CONTRACT)
    assert result.extractor == "pdfplumber"
    assert result.page_count == 2
    assert [(u.page, u.unit_index) for u in result.units] == [
        (1, 0), (1, 1), (1, 2), (2, 0), (2, 1),
    ]


def test_lines_of_one_paragraph_stay_together_and_paragraphs_are_split():
    result = _extract(CONTRACT)
    assert [u.text for u in result.units] == [
        "Contract Title",
        "1. The supplier shall deliver\nthe goods within 30 days.",
        "2. Payment is due on receipt\nof the invoice.",
        "Page two begins here.\nSecond line of the same paragraph.",
        "A separate paragraph far below.",
    ]


def test_block_numbers_restart_on_every_page():
    result = _extract(CONTRACT)
    assert [u.unit_index for u in result.units if u.page == 2] == [0, 1]


def test_units_are_labelled_as_the_files_own_text_not_as_recognised_text():
    assert {u.unit_label for u in _extract(CONTRACT).units} == {"text_block"}
    assert not any(u.needs_ocr for u in _extract(CONTRACT).units)


def test_the_plain_text_is_the_blocks_joined_so_nothing_is_lost():
    result = _extract(CONTRACT)
    assert result.text == "\n\n".join(u.text for u in result.units)


def test_the_gap_that_makes_a_new_paragraph_is_relative_to_the_line_height():
    # The same 20-point gap is a paragraph break for small type and not for large type.
    small = text_pdf([[(72, 72, 8, "small one"), (72, 92, 8, "small two")]])
    large = text_pdf([[(72, 72, 24, "large one"), (72, 102, 24, "large two")]])
    assert len(_extract(small).units) == 2
    assert len(_extract(large).units) == 1


def test_reading_order_follows_the_page_not_the_order_the_file_was_written():
    # Drawn bottom first, top second.
    pdf = text_pdf(
        [[(72, 300, 11, "written first, low on the page"), (72, 72, 11, "written last, high")]]
    )
    assert [u.text for u in _extract(pdf).units] == [
        "written last, high",
        "written first, low on the page",
    ]


# -- pages with no text ------------------------------------------------------------------


def test_a_page_with_no_text_is_flagged_for_reading_not_dropped():
    pdf = text_pdf([[(72, 72, 12, "Page one has text.")], [], [(72, 72, 12, "Page three too.")]])
    result = _extract(pdf)
    assert [(u.page, u.needs_ocr, u.text) for u in result.units] == [
        (1, False, "Page one has text."),
        (2, True, ""),
        (3, False, "Page three too."),
    ]
    assert result.text == "Page one has text.\n\n\n\nPage three too."


def test_a_scanned_document_comes_back_as_pages_to_read_not_as_an_error():
    result = _extract(scanned_pdf([_png("white"), _png("grey"), _png("black")]))
    assert result.page_count == 3
    assert [(u.page, u.needs_ocr) for u in result.units] == [(1, True), (2, True), (3, True)]
    assert result.text == ""


def test_blank_pages_are_also_pages_to_read():
    result = _extract(blank_pdf(2))
    assert [u.needs_ocr for u in result.units] == [True, True]


# -- failure and fallback ----------------------------------------------------------------


@pytest.mark.parametrize("blob", [b"", b"not a pdf at all", b"%PDF-1.4\ngarbage"])
def test_a_file_that_is_not_a_pdf_is_refused_with_extract_error(blob):
    with pytest.raises(ExtractError):
        _extract(blob)


def test_if_the_positioned_reader_fails_the_page_text_is_still_recovered(monkeypatch):
    def boom(_blob):
        raise RuntimeError("library failure")

    monkeypatch.setattr(extract, "_extract_pdf_positioned", boom)
    result = _extract(CONTRACT)
    assert result.extractor == "pypdf"
    assert [u.page for u in result.units] == [1, 2]
    assert [u.unit_label for u in result.units] == ["page", "page"]
    assert "Contract Title" in result.text and "Page two begins here." in result.text


def test_one_page_that_fails_does_not_fail_the_document(monkeypatch):
    import pdfplumber.page

    real = pdfplumber.page.Page.extract_text_lines
    calls = {"n": 0}

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("bad page")
        return real(self, *a, **k)

    monkeypatch.setattr(pdfplumber.page.Page, "extract_text_lines", flaky)
    result = _extract(CONTRACT)
    assert [(u.page, u.needs_ocr) for u in result.units][0] == (1, True)
    assert any(u.page == 2 and u.text for u in result.units)


def test_a_very_long_document_is_cut_at_the_page_limit(monkeypatch):
    monkeypatch.setattr(extract, "MAX_PDF_PAGES", 3)
    pdf = text_pdf([[(72, 72, 11, f"page {n}")] for n in range(1, 7)])
    result = _extract(pdf)
    assert result.page_count == 6  # the real count is reported
    assert max(u.page for u in result.units) == 3  # but only three were read


# -- other formats are untouched ---------------------------------------------------------


def test_other_formats_still_return_their_text_and_no_positions_yet():
    result = extract_text(blob=b"hello world", filename="n.txt", mime_type="text/plain")
    assert result.text == "hello world" and result.units == ()
