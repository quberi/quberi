"""Say where in its file a passage is, in words a reader can check against the original.

A passage is found by what the file's format lets a reader count or find:

* PDF: the page ("page 3");
* PowerPoint: the slide ("slide 4");
* Excel: the sheet and the row, numbered as Excel numbers them ("sheet Prices, row 12");
* Word: the heading it sits under and its paragraph number ("under 3.2 Payment, paragraph 12");
* text and Markdown files: the line, as an editor numbers lines ("lines 40-52").

The fields that carry this are the same for every format (``page_*``, ``unit_*``,
``unit_label``, ``section``); this module is the one place that turns them into words, so the
chat prompt, the tool results and the screen cannot disagree about what a number means.

``section`` comes from the document (a heading, a sheet name), so it is cleaned here: line
breaks and runs of spaces become one space, and it is cut short.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_SECTION_CHARS = 200

# unit_label values whose numbers are not pages.
_UNIT_KINDS = {
    "slide": "slide",
    "sheet_row": "sheet_row",
    "paragraph": "paragraph",
    "line": "line",
}


@dataclass(frozen=True)
class Place:
    """``kind`` is "page", "slide", "sheet_row", "paragraph", "line", or "" when unknown."""

    kind: str
    start: int | None = None
    end: int | None = None
    section: str = ""

    @property
    def known(self) -> bool:
        return self.kind != ""


def clean_section(section: str | None) -> str:
    text = " ".join((section or "").split())
    if len(text) > MAX_SECTION_CHARS:
        text = text[: MAX_SECTION_CHARS - 1].rstrip() + "…"
    return text


def place_of(
    *,
    page_start: int | None,
    page_end: int | None,
    unit_start: int | None,
    unit_end: int | None,
    unit_label: str = "",
    section: str = "",
) -> Place:
    """What the stored numbers of a passage mean."""

    kind = _UNIT_KINDS.get(unit_label or "")
    name = clean_section(section)
    if kind == "slide":
        return Place("slide", page_start, page_end, name) if page_start is not None else Place("")
    if kind in ("sheet_row", "paragraph", "line"):
        if unit_start is None:
            return Place(kind, None, None, name) if name else Place("")
        return Place(kind, unit_start, unit_end, name)
    if page_start is not None:
        return Place("page", page_start, page_end, "")
    return Place("")


def _range(start: int | None, end: int | None) -> tuple[str, bool]:
    """``"3"`` or ``"3-5"``, and whether it is a range."""

    if start is None:
        return "", False
    if end is None or end == start:
        return str(start), False
    return f"{start}-{end}", True


def place_zh(place: Place) -> str:
    """For the chat prompt. A page reads exactly as it always has."""

    span, _ = _range(place.start, place.end)
    if place.kind == "page":
        return f"第{span}页"
    if place.kind == "slide":
        return f"第{span}张幻灯片"
    if place.kind == "line":
        return f"第{span}行"
    if place.kind == "sheet_row":
        sheet = f"工作表「{place.section}」" if place.section else ""
        # A chunk that crossed from one sheet into the next has no single row range.
        if " / " in place.section or not span:
            return sheet or "工作表"
        return f"{sheet}第{span}行"
    if place.kind == "paragraph":
        under = f"「{place.section}」" if place.section else ""
        return f"{under}第{span}段" if span else under
    return ""


def place_en(place: Place) -> str:
    """For tool results."""

    span, many = _range(place.start, place.end)
    if place.kind == "page":
        return f"pages {span}" if many else f"page {span}"
    if place.kind == "slide":
        return f"slides {span}" if many else f"slide {span}"
    if place.kind == "line":
        return f"lines {span}" if many else f"line {span}"
    if place.kind == "sheet_row":
        sheet = f'sheet "{place.section}"' if place.section else "sheet"
        if " / " in place.section or not span:
            return sheet
        return f"{sheet}, {'rows' if many else 'row'} {span}"
    if place.kind == "paragraph":
        under = f'under "{place.section}"' if place.section else ""
        number = f"{'paragraphs' if many else 'paragraph'} {span}" if span else ""
        return ", ".join(part for part in (under, number) if part)
    return ""
