"""Tie a picture that was found by what it shows to the passage found in its text.

A picture can be found twice for one question: by the picture model (what it looks like,
offered beside the answer with a preview) and by the text read out of it (a passage like any
other, which the answer may quote). They are the same file, so the reader should be told so
instead of being shown two unrelated results.

Neither result is dropped. The picture is what has a preview; the passage is what the answer
quotes and cites. Each picture only gets a pointer to the passage(s) from its own document.
Nothing here looks anything up, so it adds no access path: it only compares ids of results
that were already allowed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def passage_numbers_by_document(document_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[int]]:
    """Document id → the 1-based positions (in the order given) of its passages."""

    found: dict[uuid.UUID, list[int]] = {}
    for position, document_id in enumerate(document_ids, start=1):
        found.setdefault(document_id, []).append(position)
    return found


def link_chat_pictures(
    pictures: Sequence[Mapping[str, Any]], citations: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Add ``citation_index`` to each stored picture match whose file is also a citation.

    ``citations`` are the dicts saved with the answer (``document_id`` and 1-based ``index``).
    The first citation of the file wins; a picture with none is returned unchanged.
    """

    first: dict[str, int] = {}
    for c in citations:
        document_id, index = c.get("document_id"), c.get("index")
        if isinstance(document_id, str) and isinstance(index, int):
            first.setdefault(document_id, index)
    linked: list[dict[str, Any]] = []
    for p in pictures:
        item = dict(p)
        index = first.get(str(p.get("document_id")))
        if index is not None:
            item["citation_index"] = index
        linked.append(item)
    return linked
