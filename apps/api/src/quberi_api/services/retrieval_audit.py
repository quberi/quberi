"""What the model could have seen: document ids recorded for each answer.

An audit has to answer "which documents were put in front of the model for
this question", not only "which ones the answer cited". So each answer's audit
event records the id of every document that came back from retrieval.

**Ids only.** No text, no names, no scores. The ids are enough to look a
document up later and keep the audit row small and free of document content.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# A single answer retrieves a handful of documents; the cap only guards the
# audit row against an agent that searches many times in one turn.
MAX_RECORDED_DOCUMENT_IDS = 50


def _unique(ids: Iterable[Any]) -> list[str]:
    """De-duplicate, keeping first-seen (rank) order, and cap the length."""

    seen: set[str] = set()
    out: list[str] = []
    for raw in ids:
        if raw is None:
            continue
        value = str(raw)
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
        if len(out) >= MAX_RECORDED_DOCUMENT_IDS:
            break
    return out


def document_ids_from_chunks(chunks: Iterable[Any]) -> list[str]:
    """Ids of the documents behind retrieved chunks (plain RAG path)."""

    return _unique(getattr(c, "document_id", None) for c in chunks)


def document_ids_from_trace(trace: Iterable[dict] | None) -> list[str]:
    """Ids of documents the knowledge-search tool returned (agent path).

    Reads the agent trace that is already stored with the answer. Only
    ``search_knowledge`` results carry passages; every other step is skipped.
    """

    def _gen() -> Iterable[Any]:
        for step in trace or []:
            if not isinstance(step, dict):
                continue
            if step.get("kind") != "tool_result" or step.get("tool") != "search_knowledge":
                continue
            result = step.get("result")
            if not isinstance(result, dict):
                continue
            for passage in result.get("passages") or []:
                if isinstance(passage, dict):
                    yield passage.get("document_id")

    return _unique(_gen())
