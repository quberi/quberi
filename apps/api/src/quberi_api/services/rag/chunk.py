"""Chunk extracted text into overlapping windows for embedding.

Strategy
--------
We use a *paragraph-then-window* approach that beats fixed-token
slicing for retrieval quality with almost no code:

1. Split on double newlines (paragraphs / slides / sheet blocks).
2. Greedily pack paragraphs into windows of at most
   ``max_chars`` characters. Very long paragraphs (contracts, tables)
   are further split on single newlines and sentence terminators.
3. Add ``overlap_chars`` characters of leading context from the
   previous window so retrieval catches queries that straddle a
   paragraph boundary.

Why characters, not tokens
--------------------------
Character length is a fine proxy for token count at CJK ratios of
~2 chars/token; we're not close to real limits and can revisit with a
tokenizer if we start using a model with a tighter window. Skipping
``tiktoken`` avoids another dep and keeps the pipeline provider-agnostic.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import List, NamedTuple

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Chunk:
    """One chunk destined for the vector store.

    ``index`` is its zero-based position within the document. ``char_start``
    / ``char_end`` refer to offsets in the *original extracted text* (not
    the source blob), useful for showing highlighted citation snippets
    later without re-extracting.
    """

    index: int
    text: str
    char_start: int
    char_end: int
    # Where in the source document this text came from, when the extractor recorded it
    # (see ``extract.TextUnit``). A chunk can span several blocks and pages, so these are
    # the first and last. ``None`` when positions are unknown (plain text, other formats).
    page_start: int | None = None
    page_end: int | None = None
    unit_start: int | None = None
    unit_end: int | None = None
    # What the numbers mean: "text_block" (a PDF's own text; ``page_*`` are pages),
    # "ocr_block" (recognised from a picture, may contain errors), "slide" (``page_*`` are
    # slide numbers), "paragraph" (a Word document's paragraph numbers), "sheet_row" (row
    # numbers on the sheet named in ``section``) or "line" (line numbers of a text file).
    # Empty when positions are unknown.
    unit_label: str = ""
    # The named part of the document the text sits in: the heading above a Word or Markdown
    # passage, the sheet of a spreadsheet. Several, joined with " / ", when a chunk crosses
    # from one into the next. Empty when the format has none.
    section: str = ""


class Source(NamedTuple):
    """A place in the source document.

    ``page`` is the page (or slide) number, ``None`` when the format has none; ``index`` is
    the block's number (its first line or row, where the format counts those); ``end`` is the
    last line or row when the block spans several (``None``: just ``index``). ``section`` is
    the named part it sits in. A plain ``(page, index, label)`` tuple converts to this.
    """

    page: int | None
    index: int
    label: str
    end: int | None = None
    section: str = ""

MAX_SECTION_CHARS = 200

# Default budget: ~500 tokens with 50-token overlap at CJK ratios.
DEFAULT_MAX_CHARS = 1200
DEFAULT_OVERLAP_CHARS = 120
MIN_CHUNK_CHARS = 40  # discard trivial windows

# Match paragraph breaks (2+ newlines) and single hard breaks.
_PARA_SPLIT = re.compile(r"\n\s*\n")
_HARD_BREAK = re.compile(r"\n")
_SENT_TERMINATOR = re.compile(r"(?<=[。！？!?.\n])\s+")


def chunk_text(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> List[Chunk]:
    """Chunk a body of text. Returns an empty list on empty input."""

    if not text:
        return []
    _check_sizes(max_chars, overlap_chars)
    paragraphs = _split_paragraphs(text, max_chars=max_chars)
    return _pack([(p, ()) for p in paragraphs], max_chars, overlap_chars)


def chunk_units(
    units: Sequence[tuple[str, Source]],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[Chunk]:
    """Chunk blocks of text that know where in the document they came from.

    Packs exactly as :func:`chunk_text` does (the same function does the work), and in
    addition records on every chunk the first and last page and block it touches, so a
    citation can say "page 3, blocks 2 to 4". ``units`` is ``(text, source)`` in reading
    order; blocks with no text are skipped by the caller.

    The tail repeated from the previous window counts too: if it came from an earlier
    block, that block is part of this chunk's range. A citation that names fewer places
    than the text really came from would send a reader to the wrong page.
    """

    # A block is one paragraph unless it is longer than a window, in which case the packer
    # splits it and every piece keeps the block's source.
    items = [
        (text.strip(), (Source(*source),)) for text, source in units if text and text.strip()
    ]
    if not items:
        return []
    _check_sizes(max_chars, overlap_chars)
    return _pack(items, max_chars, overlap_chars)


def _check_sizes(max_chars: int, overlap_chars: int) -> None:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be in [0, max_chars)")


def _position(source: Source) -> tuple[int, int]:
    """Sort key for a source: by page, then block. A source without a page sorts first."""

    return (source[0] if source[0] is not None else -1, source[1])


def _range_of(
    sources: list[Source],
) -> tuple[int | None, int | None, int | None, int | None, str, str]:
    """First and last page and block among ``sources``, the label of the text, its section.

    The label is ``ocr_block`` if any part was recognised from a picture (a citation must
    not look more certain than its least certain part), otherwise the sources' own label.
    The last block ends where its own last line or row does. The section is every distinct
    section the text touched, in reading order, joined by " / ".
    """

    if not sources:
        return None, None, None, None, "", ""
    ordered = sorted(sources, key=_position)
    first, last = ordered[0], ordered[-1]
    label = "ocr_block" if any(s[2] == "ocr_block" for s in sources) else sources[0][2]
    names: list[str] = []
    for s in ordered:
        if s.section and s.section not in names:
            names.append(s.section)
    section = " / ".join(names)
    if len(section) > MAX_SECTION_CHARS:
        section = section[: MAX_SECTION_CHARS - 1].rstrip() + "…"
    unit_end = last.end if last.end is not None else last.index
    return first.page, last.page, first.index, max(unit_end, last.index), label, section


def _pack(
    paragraphs: Sequence[tuple[str, tuple[Source, ...]]], max_chars: int, overlap_chars: int
) -> list[Chunk]:
    """Greedy packing into windows with a repeated tail. The one place this is done."""

    chunks: list[Chunk] = []
    # Each piece in the buffer remembers where it came from: one source for a block, several
    # for the repeated tail (which can span more than one block). Empty if unknown.
    buffer: list[tuple[str, tuple[Source, ...]]] = []
    buffer_len = 0
    buffer_start_offset = 0
    cursor = 0  # running index into ``text`` — approximate but stable

    def flush():
        nonlocal buffer, buffer_len, buffer_start_offset
        if not buffer:
            return
        window_text = "\n\n".join(piece for piece, _ in buffer).strip()
        # Drop trivial windows *unless* it's the only chunk we'd emit —
        # a 26-byte demo file still deserves to be searchable rather
        # than silently zero-chunked.
        if len(window_text) < MIN_CHUNK_CHARS and chunks:
            buffer = []
            buffer_len = 0
            return
        if not window_text:
            buffer = []
            buffer_len = 0
            return
        idx = len(chunks)
        start = buffer_start_offset
        end = start + len(window_text)
        known = [src for _, srcs in buffer for src in srcs]
        p_start, p_end, u_start, u_end, label, section = _range_of(known)
        chunks.append(
            Chunk(
                index=idx, text=window_text, char_start=start, char_end=end,
                page_start=p_start, page_end=p_end, unit_start=u_start, unit_end=u_end,
                unit_label=label, section=section,
            )
        )
        # Seed the next buffer with the tail of this one (overlap).
        if overlap_chars > 0 and len(window_text) > overlap_chars:
            tail = window_text[-overlap_chars:]
            buffer = [(tail, _tail_sources(buffer, overlap_chars))]
            buffer_len = len(tail)
            buffer_start_offset = end - len(tail)
        else:
            buffer = []
            buffer_len = 0
            buffer_start_offset = end

    for para, source in paragraphs:
        # If a single paragraph is bigger than the window, split it.
        if len(para) > max_chars:
            # Flush what we have first so the giant paragraph stands on its own.
            flush()
            for sub in _split_giant(para, max_chars=max_chars):
                if not buffer:
                    buffer_start_offset = cursor
                projected = buffer_len + (2 if buffer else 0) + len(sub)
                if projected > max_chars and buffer:
                    flush()
                    if not buffer:
                        buffer_start_offset = cursor
                buffer.append((sub, source))
                buffer_len += len(sub) + (2 if len(buffer) > 1 else 0)
                cursor += len(sub) + 2
                if buffer_len >= max_chars:
                    flush()
            continue

        # Normal paragraph — try to pack.
        projected = buffer_len + (2 if buffer else 0) + len(para)
        if projected > max_chars and buffer:
            flush()
        if not buffer:
            buffer_start_offset = cursor
        buffer.append((para, source))
        buffer_len += len(para) + (2 if len(buffer) > 1 else 0)
        cursor += len(para) + 2

    flush()
    return chunks


def _tail_sources(
    buffer: list[tuple[str, tuple[Source, ...]]], overlap_chars: int
) -> tuple[Source, ...]:
    """Every block the repeated tail came from.

    The tail is the last ``overlap_chars`` characters of the window. Walk the pieces from
    the end, counting the two-character separator between them, and collect the sources of
    each piece the tail reaches into. It can reach several: a short block packed after a
    longer one fits in the tail whole. All of them are kept, because a citation that names
    fewer places than the text really came from sends a reader to the wrong page.
    """

    remaining = overlap_chars
    touched: list[Source] = []
    for piece, sources in reversed(buffer):
        if remaining <= 0:
            break
        touched.extend(sources)
        remaining -= len(piece) + 2  # the piece and the separator before it
    return tuple(touched)


def _split_paragraphs(text: str, *, max_chars: int) -> list[str]:
    parts = [p.strip() for p in _PARA_SPLIT.split(text) if p.strip()]
    # If the text has zero paragraph breaks, we still get one huge blob;
    # downstream _split_giant handles it.
    return parts


def _split_giant(text: str, *, max_chars: int) -> list[str]:
    # Try hard breaks first, then sentence terminators, then character
    # slicing as the last resort.
    pieces = [p for p in _HARD_BREAK.split(text) if p.strip()]
    result: list[str] = []
    for piece in pieces:
        if len(piece) <= max_chars:
            result.append(piece)
            continue
        for sent in _SENT_TERMINATOR.split(piece):
            if len(sent) <= max_chars:
                if sent.strip():
                    result.append(sent)
            else:
                # Character-window fallback.
                for i in range(0, len(sent), max_chars):
                    frag = sent[i : i + max_chars]
                    if frag.strip():
                        result.append(frag)
    return result
