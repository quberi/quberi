"""Extract plain text from a document blob.

Format matrix (H-1)
-------------------
| ext / mime            | extractor            | notes                          |
|-----------------------|----------------------|--------------------------------|
| .pdf                  | ``pdfplumber``       | Page and block positions kept;  |
|                       | (``pypdf`` fallback) | a page with no text is flagged  |
|                       |                      | for OCR, see ``needs_ocr``.     |
| .docx                 | ``python-docx``      | Paragraphs + tables (cells).    |
| .pptx                 | ``python-pptx``      | Slide title + body text frames. |
| .xlsx                 | ``openpyxl``         | Sheet-scoped cell dump.         |
| .txt / .md / .json    | stdlib decode        | ``utf-8`` w/ error='replace'.   |
| image/*, scan PDFs    | not supported        | Falls back to a "no text" flag  |
|                       |                      | so the caller can mark the doc  |
|                       |                      | ``indexing_state='failed'`` w/  |
|                       |                      | a specific error.               |

A page whose text layer is empty is not an error here: it comes back as a unit with
``needs_ocr=True`` and no text. Reading it is asynchronous work done by the indexer
(``services/ai/ocr``), so this module stays synchronous and cheap.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from typing import Callable, Optional

_log = logging.getLogger(__name__)


class ExtractError(RuntimeError):
    """Raised when a document cannot be extracted (bad file, unsupported)."""


@dataclass(frozen=True)
class TextUnit:
    """One block of text and where it sits in the source document.

    ``page`` is the 1-based page (or slide) for formats that have them, else ``None``.
    ``unit_index`` counts blocks within the page (0-based), or, for formats without pages,
    is the block's own number as a reader would count it (paragraph, sheet row, first line).
    ``unit_label`` says what kind of block it is: ``text_block`` is text from a PDF's text
    layer, ``ocr_block`` is text recognised from a picture of the page and may contain
    errors, ``slide``, ``paragraph``, ``sheet_row`` and ``line`` are the other formats'.
    """

    text: str
    page: int | None
    unit_index: int
    unit_label: str = "text_block"
    # A page with no text layer at all. ``text`` is empty; the indexer reads the page.
    needs_ocr: bool = False
    # The last line or row of a block that spans several (``unit_index`` is the first);
    # ``None`` for a block that is just one.
    unit_end: int | None = None
    # The named part of the document the block sits in: the heading above it, the sheet.
    section: str = ""


@dataclass(frozen=True)
class ExtractResult:
    """Text pulled out of one document, plus a few useful stats."""

    text: str
    page_count: int  # -1 when the format has no pages
    extractor: str  # human-readable, e.g. "pypdf", "python-docx"
    # The same text split into positioned blocks. Empty for formats that do not yet record
    # positions; ``text`` is always complete either way.
    units: tuple[TextUnit, ...] = ()


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------


def extract_text(
    *,
    blob: bytes,
    filename: str,
    mime_type: str,
) -> ExtractResult:
    """Route a blob through the extractor that best matches its type.

    We prefer ``mime_type`` when it's specific, and fall back to the
    filename extension when it's ``application/octet-stream`` (the
    upload API's default when the browser didn't sniff).
    """

    mime = (mime_type or "").lower()
    lower_name = filename.lower()

    dispatcher = _pick_extractor(mime, lower_name)
    try:
        return dispatcher(blob)
    except ExtractError:
        raise
    except Exception as exc:  # noqa: BLE001 — external libs raise varied types
        raise ExtractError(
            f"{filename}: extractor {dispatcher.__name__} raised {type(exc).__name__}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


Extractor = Callable[[bytes], ExtractResult]


def _pick_extractor(mime: str, name: str) -> Extractor:
    # PDF
    if mime == "application/pdf" or name.endswith(".pdf"):
        return _extract_pdf
    # DOCX
    if mime == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ) or name.endswith(".docx"):
        return _extract_docx
    # PPTX
    if mime == (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ) or name.endswith(".pptx"):
        return _extract_pptx
    # XLSX
    if mime == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ) or name.endswith(".xlsx"):
        return _extract_xlsx
    # Anything text-shaped.
    if (
        mime.startswith("text/")
        or mime == "application/json"
        or name.endswith((".txt", ".md", ".markdown", ".log", ".csv", ".tsv", ".json"))
    ):
        if mime == "text/markdown" or name.endswith((".md", ".markdown")):
            return _extract_markdown
        return _extract_plaintext
    raise ExtractError(
        f"no extractor for mime={mime!r} filename={name!r} — "
        "OCR / images / scanned PDFs land in H-3"
    )


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------


# A paragraph break is a gap between two lines of more than this fraction of a line's height.
# Lines of one paragraph sit a few points apart (about 0.3 of their height); paragraphs and
# headings are separated by roughly twice the line height. Measured on test pages, and to
# be checked again on real documents.
_PARAGRAPH_GAP = 0.8
# Pages read per document. A huge or hostile PDF must not hold the worker for hours; pages
# beyond this are left out and the result says so.
MAX_PDF_PAGES = 1000


def _group_lines(lines: list[dict]) -> list[str]:
    """Join lines into paragraph-sized blocks by the gap between them."""

    blocks: list[list[str]] = []
    previous = None
    for line in sorted(lines, key=lambda ln: (round(ln["top"], 1), ln["x0"])):
        text = (line.get("text") or "").strip()
        if not text:
            continue
        height = max(line["bottom"] - line["top"], 1.0)
        starts_new = (
            previous is None or (line["top"] - previous["bottom"]) > _PARAGRAPH_GAP * height
        )
        if starts_new:
            blocks.append([text])
        else:
            blocks[-1].append(text)
        previous = line
    return ["\n".join(block) for block in blocks]


def _extract_pdf(blob: bytes) -> ExtractResult:
    try:
        return _extract_pdf_positioned(blob)
    except ExtractError:
        raise
    except Exception as exc:  # noqa: BLE001 — a library failure must not lose the document
        _log.warning("pdfplumber failed (%s: %s); falling back to pypdf", type(exc).__name__, exc)
        return _extract_pdf_pages_only(blob)


def _extract_pdf_positioned(blob: bytes) -> ExtractResult:
    import logging as _logging

    import pdfplumber

    # pdfminer logs every font quirk at WARNING; that is noise for us.
    _logging.getLogger("pdfminer").setLevel(_logging.ERROR)

    units: list[TextUnit] = []
    page_texts: list[str] = []
    with pdfplumber.open(io.BytesIO(blob)) as pdf:
        total_pages = len(pdf.pages)
        for number, page in enumerate(pdf.pages[:MAX_PDF_PAGES], start=1):
            try:
                blocks = _group_lines(page.extract_text_lines())
            except Exception as exc:  # noqa: BLE001 — one bad page must not fail the document
                _log.warning("pdfplumber: page %d failed to extract: %s", number, exc)
                blocks = []
            if not blocks:
                units.append(TextUnit("", number, 0, "text_block", needs_ocr=True))
                page_texts.append("")
                continue
            for index, block in enumerate(blocks):
                units.append(TextUnit(block, number, index))
            page_texts.append("\n\n".join(blocks))
    if total_pages > MAX_PDF_PAGES:
        _log.warning("pdf has %d pages; only the first %d were read", total_pages, MAX_PDF_PAGES)
    text = "\n\n".join(page_texts).strip()
    if not text and not any(u.needs_ocr for u in units):
        raise ExtractError("the PDF has no pages")
    return ExtractResult(
        text=text, page_count=total_pages, extractor="pdfplumber", units=tuple(units)
    )


def _extract_pdf_pages_only(blob: bytes) -> ExtractResult:
    """Fallback: pypdf gives a page of text at a time, so the page is known but not the block."""

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(blob))
    units: list[TextUnit] = []
    pages: list[str] = []
    for number, page in enumerate(reader.pages[:MAX_PDF_PAGES], start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception as exc:  # noqa: BLE001 — page-level failures shouldn't fail the doc
            _log.warning("pypdf: page failed to extract: %s", exc)
            text = ""
        pages.append(text)
        units.append(TextUnit(text, number, 0, "page", needs_ocr=not text))
    text = "\n\n".join(pages).strip()
    if not text and not any(u.needs_ocr for u in units):
        raise ExtractError("the PDF has no pages")
    return ExtractResult(
        text=text, page_count=len(reader.pages), extractor="pypdf", units=tuple(units)
    )


_HEADING_STYLE = re.compile(r"^(?:heading|标题)\s*(\d+)$", re.IGNORECASE)
MAX_HEADING_CHARS = 80


def _heading_level(style_name: str) -> int | None:
    """The level of "Heading 2" / "标题 2" and so on (a "Title" is level 1); ``None`` otherwise."""

    name = (style_name or "").strip()
    if name.lower() in ("title", "标题"):
        return 1
    match = _HEADING_STYLE.match(name)
    return int(match.group(1)) if match else None


def _heading_path(stack: list[tuple[int, str]]) -> str:
    return " > ".join(title for _, title in stack)


def _extract_docx(blob: bytes) -> ExtractResult:
    """Paragraphs and table rows in the order they appear, each numbered and filed under the
    heading above it.

    A Word file has no page numbers a program can know (where a page ends depends on the
    fonts and printer), but a reader can count paragraphs and find a heading, so a passage
    is "under 3.2 Payment, paragraph 12". Empty paragraphs are not counted. A table row is
    one paragraph.
    """

    from docx import Document as DocxDocument
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = DocxDocument(io.BytesIO(blob))
    units: list[TextUnit] = []
    headings: list[tuple[int, str]] = []
    number = 0

    def add(text: str) -> None:
        nonlocal number
        number += 1
        units.append(TextUnit(text, None, number, "paragraph", section=_heading_path(headings)))

    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, doc)
            text = para.text.strip()
            if not text:
                continue
            level = _heading_level(para.style.name if para.style is not None else "")
            if level is not None:
                while headings and headings[-1][0] >= level:
                    headings.pop()
                headings.append((level, text[:MAX_HEADING_CHARS]))
            add(text)
        elif tag == "tbl":
            for row in Table(child, doc).rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells)
                if row_text.strip("| "):
                    add(row_text)
    text = "\n".join(u.text for u in units).strip()
    if not text:
        raise ExtractError("python-docx returned no text")
    return ExtractResult(
        text=text, page_count=-1, extractor="python-docx", units=tuple(units)
    )


def _extract_pptx(blob: bytes) -> ExtractResult:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(blob))
    slides: list[str] = []
    units: list[TextUnit] = []
    for idx, slide in enumerate(prs.slides, start=1):
        chunks: list[str] = []
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for para in shape.text_frame.paragraphs:
                text = "".join(run.text for run in para.runs).strip()
                if text:
                    chunks.append(text)
        if chunks:
            body = f"[Slide {idx}]\n" + "\n".join(chunks)
            slides.append(body)
            # The slide is the place a reader finds it by.
            units.append(TextUnit(body, idx, 0, "slide"))
    text = "\n\n".join(slides).strip()
    if not text:
        raise ExtractError("python-pptx returned no text")
    return ExtractResult(
        text=text, page_count=len(prs.slides), extractor="python-pptx", units=tuple(units)
    )


_HEADER_MAX_CELL_CHARS = 60


def _cell_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _looks_like_header(row: tuple) -> bool:
    """A header row is two or more short text cells and no numbers, dates or formulas."""

    filled = [v for v in row if _cell_text(v)]
    return (
        len(filled) >= 2
        and all(isinstance(v, str) and len(v.strip()) <= _HEADER_MAX_CELL_CHARS for v in filled)
    )


def _sheet_rows(sheet) -> list[tuple[int, str]]:
    """``(row number, line)`` for each non-empty row, numbered as the spreadsheet numbers them.

    With a header row, every data row is written as ``name: value`` pairs.

    The text is later cut into pieces for search, and a piece from the middle of a big sheet
    has no header row above it, so "35" would not say whether it is a price or a quantity.
    Putting the column name next to each value makes every row stand on its own, whichever
    piece it lands in. A sheet without a header row keeps the plain ``a | b | c`` form.
    The row number is the one on the left of the sheet in Excel, empty rows included in the
    count, so a reader can go straight to it.
    """

    header: list[str] | None = None
    rows: list[tuple[int, str]] = []
    first = True
    for number, raw in enumerate(sheet.iter_rows(min_row=1, values_only=True), start=1):
        cells = [_cell_text(v) for v in raw]
        if not any(cells):
            continue
        if first:
            first = False
            if _looks_like_header(raw):
                header = cells
                rows.append((number, " | ".join(cells)))
                continue
        if header is None:
            rows.append((number, " | ".join(cells)))
            continue
        pairs = []
        for i, value in enumerate(cells):
            if not value:
                continue
            name = header[i] if i < len(header) and header[i] else ""
            pairs.append(f"{name}: {value}" if name else value)
        rows.append((number, " | ".join(pairs)))
    return rows


def _sheet_lines(sheet) -> list[str]:
    return [line for _, line in _sheet_rows(sheet)]


def _extract_xlsx(blob: bytes) -> ExtractResult:
    from openpyxl import load_workbook

    wb = load_workbook(filename=io.BytesIO(blob), read_only=True, data_only=True)
    sheets: list[str] = []
    units: list[TextUnit] = []
    for sheet_number, sheet in enumerate(wb.worksheets, start=1):
        rows = _sheet_rows(sheet)
        if not rows:
            continue
        sheets.append(f"[Sheet: {sheet.title}]\n" + "\n".join(line for _, line in rows))
        for position, (row, line) in enumerate(rows):
            # The first row of a sheet carries the sheet's name in the text, as it always did.
            body = f"[Sheet: {sheet.title}]\n{line}" if position == 0 else line
            units.append(
                TextUnit(body, sheet_number, row, "sheet_row", section=sheet.title)
            )
    text = "\n\n".join(sheets).strip()
    if not text:
        raise ExtractError("openpyxl returned no text")
    return ExtractResult(
        text=text, page_count=len(wb.sheetnames), extractor="openpyxl", units=tuple(units)
    )


_MARKDOWN_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")


def _paragraph_blocks(text: str) -> list[tuple[int, int, list[str]]]:
    """Blocks of consecutive non-blank lines: ``(first line, last line, lines)``, numbered from
    1 as an editor numbers them. A line of only spaces is blank."""

    blocks: list[tuple[int, int, list[str]]] = []
    current: list[str] = []
    start = 0
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            if not current:
                start = number
            current.append(line)
        elif current:
            blocks.append((start, number - 1, current))
            current = []
    if current:
        blocks.append((start, start + len(current) - 1, current))
    return blocks


def _extract_plaintext(blob: bytes, *, markdown: bool = False) -> ExtractResult:
    """Text, JSON, CSV and Markdown files: paragraphs numbered by their lines in the file.

    The line numbers are those of the file as saved (the editor's), so they are counted
    before anything is trimmed. In Markdown the heading above a passage is its section.
    """

    # Decode conservatively; refuse to fail the whole document on a bad
    # byte. Markdown / txt / json / csv all pass through unchanged.
    decoded = blob.decode("utf-8", errors="replace")
    text = decoded.strip()
    if not text:
        raise ExtractError("plaintext body is empty")
    units: list[TextUnit] = []
    headings: list[tuple[int, str]] = []
    in_fence = False
    for first, last, lines in _paragraph_blocks(decoded):
        if markdown:
            for line in lines:
                if line.lstrip().startswith(("```", "~~~")):
                    in_fence = not in_fence
                match = None if in_fence else _MARKDOWN_HEADING.match(line)
                if match:
                    level = len(match.group(1))
                    while headings and headings[-1][0] >= level:
                        headings.pop()
                    headings.append((level, match.group(2)[:MAX_HEADING_CHARS]))
        units.append(
            TextUnit(
                "\n".join(lines).strip(),
                None,
                first,
                "line",
                unit_end=last,
                section=_heading_path(headings) if markdown else "",
            )
        )
    return ExtractResult(text=text, page_count=-1, extractor="stdlib", units=tuple(units))


def _extract_markdown(blob: bytes) -> ExtractResult:
    return _extract_plaintext(blob, markdown=True)


def _noop() -> Optional[Extractor]:
    """Placeholder for OCR / scanned-PDF path (H-3)."""

    return None
