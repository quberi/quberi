"""In-process embedding provider (BGE-M3, Qwen3-Embedding, …).

Runs the model directly inside the API/worker process via
``sentence-transformers``. First load fetches weights from
Hugging Face into the ``HF_HOME`` cache and holds them in memory for
the process lifetime. Subsequent embedding calls are pure compute, no
outbound network traffic.

Model → HF repo mapping
-----------------------
``bge-m3`` and ``qwen3-embedding-0.6b`` are the two we ship in the
:mod:`registry`. Any other 1024-d dense embedding model that
sentence-transformers can load will work if we add it to
:data:`_MODEL_TO_HF`.

Threading
---------
sentence-transformers is thread-unsafe at load time and CPU-bound at
inference. We serialise inference calls per-model with an asyncio lock;
throughput is bounded by the single-model call anyway, so a lock is
enough. To parallelise later we can move to a worker pool.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import List, Optional, Sequence

from quberi_api.services.ai.base import EmbeddingProvider, ProviderConfig

_log = logging.getLogger(__name__)


_MODEL_TO_HF = {
    "bge-m3": "BAAI/bge-m3",
    "qwen3-embedding-0.6b": "Qwen/Qwen3-Embedding-0.6B",
}


# One process-wide cache — loading a 2.3 GB model twice would be
# catastrophic. Keyed by model id so we can host both BGE and Qwen at
# once if two orgs pick different models.
_LOADED: dict[str, "_LoadedModel"] = {}
_LOAD_LOCK = asyncio.Lock()


class _LoadedModel:
    """A thin wrapper around a ``SentenceTransformer`` instance."""

    def __init__(self, st_model, vector_dim: int) -> None:
        self.st_model = st_model
        self.vector_dim = vector_dim
        self.inference_lock = asyncio.Lock()


async def _get_or_load(model_id: str, vector_dim: int) -> _LoadedModel:
    if model_id in _LOADED:
        return _LOADED[model_id]
    async with _LOAD_LOCK:
        # Double-check inside the lock; another coroutine may have
        # populated the cache while we were waiting.
        if model_id in _LOADED:
            return _LOADED[model_id]
        hf_repo = _MODEL_TO_HF.get(model_id)
        if hf_repo is None:
            raise KeyError(
                f"no HF repo mapping for local embedding model {model_id!r}; "
                "add it to _MODEL_TO_HF"
            )
        _log.info("loading local embedding model %s (%s)", model_id, hf_repo)
        # Deferred import — sentence-transformers pulls in torch (~1 GB).
        # Keeping this import out of module scope means the API worker
        # can start up without the model dependency until first use.
        from sentence_transformers import SentenceTransformer

        # Run the blocking load on the default thread pool so we don't
        # freeze the event loop for the multi-second model materialisation.
        loop = asyncio.get_running_loop()
        st_model = await loop.run_in_executor(
            None,
            lambda: SentenceTransformer(
                hf_repo,
                cache_folder=os.getenv("HF_HOME"),
                trust_remote_code=True,
            ),
        )
        dim = int(st_model.get_sentence_embedding_dimension() or vector_dim)
        if dim != vector_dim:
            _log.warning(
                "model %s reported dim=%d but registry says %d — trusting model",
                model_id, dim, vector_dim,
            )
        loaded = _LoadedModel(st_model, dim)
        _LOADED[model_id] = loaded
        return loaded


class LocalEmbeddingProvider(EmbeddingProvider):
    """Sentence-transformers-backed provider for BGE-M3 / Qwen3-Embedding.

    Instantiated once per resolver call — the actual model handle is
    process-cached via ``_LOADED`` so instantiation is cheap.
    """

    kind = "local_embedding"

    def __init__(self, config: ProviderConfig, *, vector_dim: int) -> None:
        self._config = config
        self._vector_dim = vector_dim

    @property
    def vector_dim(self) -> int:
        return self._vector_dim

    async def embed(
        self,
        texts: Sequence[str],
        *,
        model: str,
        instruction: Optional[str] = None,
    ) -> List[List[float]]:
        if not texts:
            return []
        loaded = await _get_or_load(model, self._vector_dim)
        # Qwen3-Embedding is instruction-aware; other models silently
        # ignore the instruction argument. Encoding a batch is CPU-bound
        # so we run it in the default thread pool.
        payload: list[str]
        if instruction and model.startswith("qwen3-embedding"):
            payload = [f"Instruct: {instruction}\nQuery: {t}" for t in texts]
        else:
            payload = list(texts)

        async with loaded.inference_lock:
            loop = asyncio.get_running_loop()
            vectors = await loop.run_in_executor(
                None,
                lambda: loaded.st_model.encode(
                    payload,
                    normalize_embeddings=True,
                    convert_to_numpy=False,
                ),
            )
        # ``encode`` returns a list of ``torch.Tensor`` when
        # ``convert_to_numpy=False``; coerce to python lists so Qdrant's
        # client (which expects ``list[float]``) is happy.
        out: List[List[float]] = []
        for v in vectors:
            if hasattr(v, "tolist"):
                out.append([float(x) for x in v.tolist()])
            else:
                out.append([float(x) for x in v])
        # Sanity: the model file may quietly disagree with the registry.
        if out and len(out[0]) != loaded.vector_dim:
            raise RuntimeError(
                f"local model {model} produced {len(out[0])}-d vectors, "
                f"expected {loaded.vector_dim}"
            )
        return out
