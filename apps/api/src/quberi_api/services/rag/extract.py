"""Extract plain text from a document blob.

Format matrix (H-1)
-------------------
| ext / mime            | extractor            | notes                          |
|-----------------------|----------------------|--------------------------------|
| .pdf                  | ``pypdf``            | Text-native PDFs only.          |
| .docx                 | ``python-docx``      | Paragraphs + tables (cells).    |
| .pptx                 | ``python-pptx``      | Slide title + body text frames. |
| .xlsx                 | ``openpyxl``         | Sheet-scoped cell dump.         |
| .txt / .md / .json    | stdlib decode        | ``utf-8`` w/ error='replace'.   |
| image/*, scan PDFs    | not supported        | Falls back to a "no text" flag  |
|                       |                      | so the caller can mark the doc  |
|                       |                      | ``indexing_state='failed'`` w/  |
|                       |                      | a specific error.               |

OCR + complex-layout PDFs land in H-3.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Callable, Optional

_log = logging.getLogger(__name__)


class ExtractError(RuntimeError):
    """Raised when a document cannot be extracted (bad file, unsupported)."""


@dataclass(frozen=True)
class ExtractResult:
    """Text pulled out of one document, plus a few useful stats."""

    text: str
    page_count: int  # -1 when the format has no pages
    extractor: str  # human-readable, e.g. "pypdf", "python-docx"


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
        return _extract_plaintext
    raise ExtractError(
        f"no extractor for mime={mime!r} filename={name!r} — "
        "OCR / images / scanned PDFs land in H-3"
    )


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------


def _extract_pdf(blob: bytes) -> ExtractResult:
    # Deferred import — pypdf imports are cheap but keep the module
    # graph lean for the API worker in case someone strips pypdf out.
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(blob))
    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001 — page-level failures shouldn't fail the doc
            _log.warning("pypdf: page failed to extract: %s", exc)
            pages.append("")
    text = "\n\n".join(pages).strip()
    if not text:
        raise ExtractError(
            "pypdf returned no text — likely a scanned PDF (OCR arrives in H-3)"
        )
    return ExtractResult(text=text, page_count=len(reader.pages), extractor="pypdf")


def _extract_docx(blob: bytes) -> ExtractResult:
    from docx import Document as DocxDocument

    doc = DocxDocument(io.BytesIO(blob))
    parts: list[str] = []
    # Paragraphs first, then tables (each cell on its own line).
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            parts.append(text)
    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join(cell.text.strip() for cell in row.cells)
            if row_text.strip("| "):
                parts.append(row_text)
    text = "\n".join(parts).strip()
    if not text:
        raise ExtractError("python-docx returned no text")
    return ExtractResult(text=text, page_count=-1, extractor="python-docx")


def _extract_pptx(blob: bytes) -> ExtractResult:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(blob))
    slides: list[str] = []
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
            slides.append(f"[Slide {idx}]\n" + "\n".join(chunks))
    text = "\n\n".join(slides).strip()
    if not text:
        raise ExtractError("python-pptx returned no text")
    return ExtractResult(text=text, page_count=len(prs.slides), extractor="python-pptx")


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


def _sheet_lines(sheet) -> list[str]:
    """One line per row. With a header row, every data row is written as ``name: value`` pairs.

    The text is later cut into pieces for search, and a piece from the middle of a big sheet
    has no header row above it, so "35" would not say whether it is a price or a quantity.
    Putting the column name next to each value makes every row stand on its own, whichever
    piece it lands in. A sheet without a header row keeps the plain ``a | b | c`` form.
    """

    header: list[str] | None = None
    lines: list[str] = []
    first = True
    for raw in sheet.iter_rows(values_only=True):
        cells = [_cell_text(v) for v in raw]
        if not any(cells):
            continue
        if first:
            first = False
            if _looks_like_header(raw):
                header = cells
                lines.append(" | ".join(cells))
                continue
        if header is None:
            lines.append(" | ".join(cells))
            continue
        pairs = []
        for i, value in enumerate(cells):
            if not value:
                continue
            name = header[i] if i < len(header) and header[i] else ""
            pairs.append(f"{name}: {value}" if name else value)
        lines.append(" | ".join(pairs))
    return lines


def _extract_xlsx(blob: bytes) -> ExtractResult:
    from openpyxl import load_workbook
    wb = load_workbook(filename=io.BytesIO(blob), read_only=True, data_only=True)
    sheets: list[str] = []
    for sheet in wb.worksheets:
        rows = _sheet_lines(sheet)
        if rows:
            sheets.append(f"[Sheet: {sheet.title}]\n" + "\n".join(rows))
    text = "\n\n".join(sheets).strip()
    if not text:
        raise ExtractError("openpyxl returned no text")
    return ExtractResult(
        text=text, page_count=len(wb.sheetnames), extractor="openpyxl"
    )


def _extract_plaintext(blob: bytes) -> ExtractResult:
    # Decode conservatively; refuse to fail the whole document on a bad
    # byte. Markdown / txt / json / csv all pass through unchanged.
    text = blob.decode("utf-8", errors="replace").strip()
    if not text:
        raise ExtractError("plaintext body is empty")
    return ExtractResult(text=text, page_count=-1, extractor="stdlib")


def _noop() -> Optional[Extractor]:
    """Placeholder for OCR / scanned-PDF path (H-3)."""

    return None
