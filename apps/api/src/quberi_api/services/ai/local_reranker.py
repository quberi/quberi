"""In-process reranker (BGE-reranker-v2-m3).

Rerankers are the "second pass" of retrieval — you over-fetch top-30
candidates cheaply via dense search, then a cross-encoder scores each
``(query, chunk)`` pair jointly and reorders. Result: much better
precision-at-K for the top handful the LLM actually sees.

Currently unused by default; ``retrieve_for_conversation`` picks it up
when the org has a ``reranker`` selection in ``ai_model_selections``.
Runs on CPU via ``sentence-transformers.CrossEncoder``; the BGE-reranker
weights are ~570 MB and cached in the same ``HF_HOME`` volume as the
embedding model.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import List, Sequence

_log = logging.getLogger(__name__)


_MODEL_TO_HF = {
    "bge-reranker-v2-m3": "BAAI/bge-reranker-v2-m3",
}

_LOADED: dict[str, "_LoadedReranker"] = {}
_LOAD_LOCK = asyncio.Lock()


@dataclass
class _LoadedReranker:
    ce_model: object
    inference_lock: asyncio.Lock


async def _get_or_load(model_id: str) -> _LoadedReranker:
    if model_id in _LOADED:
        return _LOADED[model_id]
    async with _LOAD_LOCK:
        if model_id in _LOADED:
            return _LOADED[model_id]
        repo = _MODEL_TO_HF.get(model_id)
        if repo is None:
            raise KeyError(f"no HF repo for reranker {model_id!r}")
        _log.info("loading reranker %s (%s)", model_id, repo)
        from sentence_transformers import CrossEncoder

        loop = asyncio.get_running_loop()
        ce = await loop.run_in_executor(
            None,
            lambda: CrossEncoder(
                repo,
                max_length=512,
                cache_folder=os.getenv("HF_HOME"),
                trust_remote_code=True,
            ),
        )
        loaded = _LoadedReranker(ce_model=ce, inference_lock=asyncio.Lock())
        _LOADED[model_id] = loaded
        return loaded


class LocalRerankerProvider:
    """Wraps ``sentence-transformers.CrossEncoder`` for our provider protocol.

    Distinct from ``EmbeddingProvider`` / ``ChatProvider`` — reranking
    doesn't fit either. Callers (retrieval layer) query it directly
    via :meth:`rerank`.
    """

    kind = "local_reranker"

    def __init__(self, *, model_id: str) -> None:
        self._model_id = model_id

    async def rerank(
        self,
        *,
        query: str,
        candidates: Sequence[str],
    ) -> List[float]:
        """Return per-candidate scores in the same order as ``candidates``.

        Empty list in → empty list out; higher = more relevant.
        """

        if not candidates:
            return []
        loaded = await _get_or_load(self._model_id)
        pairs = [(query, c) for c in candidates]
        async with loaded.inference_lock:
            loop = asyncio.get_running_loop()
            scores = await loop.run_in_executor(
                None, lambda: loaded.ce_model.predict(pairs)
            )
        # ``predict`` returns np.ndarray on batch input; coerce.
        return [float(s) for s in scores]
