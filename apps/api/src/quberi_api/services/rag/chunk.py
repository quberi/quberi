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
from dataclasses import dataclass
from typing import List

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
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be in [0, max_chars)")

    paragraphs = _split_paragraphs(text, max_chars=max_chars)

    chunks: List[Chunk] = []
    buffer: list[str] = []
    buffer_len = 0
    buffer_start_offset = 0
    cursor = 0  # running index into ``text`` — approximate but stable

    def flush():
        nonlocal buffer, buffer_len, buffer_start_offset
        if not buffer:
            return
        window_text = "\n\n".join(buffer).strip()
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
        chunks.append(
            Chunk(index=idx, text=window_text, char_start=start, char_end=end)
        )
        # Seed the next buffer with the tail of this one (overlap).
        if overlap_chars > 0 and len(window_text) > overlap_chars:
            tail = window_text[-overlap_chars:]
            buffer = [tail]
            buffer_len = len(tail)
            buffer_start_offset = end - len(tail)
        else:
            buffer = []
            buffer_len = 0
            buffer_start_offset = end

    for para in paragraphs:
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
                buffer.append(sub)
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
        buffer.append(para)
        buffer_len += len(para) + (2 if len(buffer) > 1 else 0)
        cursor += len(para) + 2

    flush()
    return chunks


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
