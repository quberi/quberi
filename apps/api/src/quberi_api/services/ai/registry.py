"""Canonical model catalog.

The database persists a free-string ``model_id`` per selection; this
registry is the source of truth for *what that string means*. New
models are added by appending to :data:`MODEL_REGISTRY`; the migration
path for switching an org from BGE-M3 to Qwen3-Embedding-0.6B is a
config change + a re-index, no code redeploy.

Every entry declares:

* ``model_id`` — the string persisted in ``ai_model_selections.model_id``.
* ``provider_kind`` — which :class:`ChatProvider` / :class:`EmbeddingProvider`
  subclass serves it (must match a class registered in the resolver).
* ``purpose`` — one of ``chat`` / ``embedding`` / ``reranker`` (a model that reads
  pictures is a ``chat`` model with ``supports_vision``; "vision" is only the name of the
  organisation's separate choice of which one describes pictures).
* ``vector_dim`` — required for embeddings (Qdrant collection sizing).
* ``context_window`` — advisory, used by the chunker + retriever budgets.
* ``notes`` — human-readable pointer for the settings UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    provider_kind: str
    purpose: str  # "chat" | "embedding" | "reranker"
    display_name: str
    vector_dim: Optional[int] = None  # required for embeddings
    context_window: int = 4096
    notes: str = ""
    # Which company serves this model — e.g. "deepseek", "openai",
    # "dashscope", "moonshot". Distinguishes vendors that share the same
    # ``provider_kind`` (several speak the OpenAI-compatible wire
    # protocol). ``None`` for locally-run models (no external vendor).
    # Used to match a model against the right platform-default binding
    # (see ``services.ai.resolver``) and to label it in the UI.
    vendor: Optional[str] = None
    # Some models reject any ``temperature`` other than one fixed value
    # (e.g. Kimi K2.x only accepts 1). When set, the provider overrides
    # the caller's temperature with this value instead of failing with 400.
    fixed_temperature: Optional[float] = None
    # Whether the model accepts pictures in a user message. Only such models are offered
    # as a "vision model" (the one that describes pictures for search). A model name an
    # administrator types in for their own endpoint is not in this registry, so it is not
    # assumed to read pictures; they declare it by choosing it as the vision model.
    supports_vision: bool = False


# ---------------------------------------------------------------------------
# Chat models — LLMs used for answer generation.
# ---------------------------------------------------------------------------

_CHAT_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        model_id="deepseek-chat",
        provider_kind="openai_compat",
        purpose="chat",
        display_name="DeepSeek Chat (V3)",
        context_window=64_000,
        notes="Base URL: https://api.deepseek.com. OpenAI-schema compatible.",
        vendor="deepseek",
    ),
    ModelSpec(
        model_id="deepseek-reasoner",
        provider_kind="openai_compat",
        purpose="chat",
        display_name="DeepSeek Reasoner (R1)",
        context_window=64_000,
        notes="Reasoning model; slower, higher quality on hard questions.",
        vendor="deepseek",
    ),
    # OpenAI — enabled the moment an org drops in a key.
    # DeepSeek's model that also reads pictures (up to 1024 tokens each; JPEG, PNG, GIF and
    # WebP; pictures only in user messages). The earlier experimental vision name is retired
    # and now routes here.
    ModelSpec(
        model_id="deepseek-flash",
        provider_kind="openai_compat",
        purpose="chat",
        display_name="DeepSeek Flash (reads pictures)",
        context_window=128_000,
        notes="Base URL: https://api.deepseek.com. Accepts pictures in user messages.",
        vendor="deepseek",
        supports_vision=True,
    ),
    ModelSpec(
        model_id="gpt-4o-mini",
        provider_kind="openai_compat",
        purpose="chat",
        display_name="OpenAI GPT-4o mini",
        context_window=128_000,
        notes="Base URL: https://api.openai.com/v1. Requires OpenAI API key.",
        vendor="openai",
        supports_vision=True,
    ),
    ModelSpec(
        model_id="gpt-4o",
        provider_kind="openai_compat",
        purpose="chat",
        display_name="OpenAI GPT-4o",
        context_window=128_000,
        vendor="openai",
        supports_vision=True,
    ),
    # Qwen via DashScope OpenAI-compat mode.
    ModelSpec(
        model_id="qwen-max",
        provider_kind="openai_compat",
        purpose="chat",
        display_name="通义千问 Max",
        context_window=32_000,
        notes="Base URL: https://dashscope.aliyuncs.com/compatible-mode/v1",
        vendor="dashscope",
    ),
    ModelSpec(
        model_id="qwen-plus",
        provider_kind="openai_compat",
        purpose="chat",
        display_name="通义千问 Plus",
        context_window=32_000,
        vendor="dashscope",
    ),
    ModelSpec(
        model_id="qwen-vl-max",
        provider_kind="openai_compat",
        purpose="chat",
        display_name="通义千问 VL Max (reads pictures)",
        context_window=32_000,
        notes="Base URL: https://dashscope.aliyuncs.com/compatible-mode/v1",
        vendor="dashscope",
        supports_vision=True,
    ),
    # Kimi / Moonshot.
    ModelSpec(
        model_id="moonshot-v1-32k",
        provider_kind="openai_compat",
        purpose="chat",
        display_name="Moonshot Kimi (32K)",
        context_window=32_000,
        notes="Base URL: https://api.moonshot.cn/v1",
        vendor="moonshot",
    ),
    ModelSpec(
        model_id="kimi-k2.6",
        provider_kind="openai_compat",
        purpose="chat",
        display_name="Moonshot Kimi K2.6",
        context_window=256_000,
        notes="Base URL: https://api.moonshot.cn/v1. Only temperature=1 is accepted.",
        vendor="moonshot",
        fixed_temperature=1.0,
    ),
)


# ---------------------------------------------------------------------------
# Embedding models — text → vectors.
# ---------------------------------------------------------------------------

_EMBEDDING_MODELS: tuple[ModelSpec, ...] = (
    # Platform default. Runs in-process via ``sentence-transformers``;
    # weights (~2.3 GB) are cached in the container on first boot.
    ModelSpec(
        model_id="bge-m3",
        provider_kind="local_embedding",
        purpose="embedding",
        display_name="BAAI BGE-M3 (local, dense-mode)",
        vector_dim=1024,
        context_window=8192,
        notes="100+ languages; hybrid retrieval also gets sparse+multi-vec (H-3).",
    ),
    # Alternative local: Qwen3-Embedding open-weights, ~1.5 GB, top MTEB.
    ModelSpec(
        model_id="qwen3-embedding-0.6b",
        provider_kind="local_embedding",
        purpose="embedding",
        display_name="Qwen3 Embedding 0.6B (local)",
        vector_dim=1024,
        context_window=32_768,
        notes="Instruction-aware. Same collection layout as BGE-M3 (both 1024-d) — "
        "still requires re-index because the vector space is different.",
    ),
    # OpenAI-hosted, for orgs that bring an OpenAI key.
    ModelSpec(
        model_id="text-embedding-3-small",
        provider_kind="openai_compat",
        purpose="embedding",
        display_name="OpenAI text-embedding-3-small",
        vector_dim=1536,
        context_window=8191,
        notes="Cheapest cloud option ($0.02 / 1M tokens).",
        vendor="openai",
    ),
    ModelSpec(
        model_id="text-embedding-3-large",
        provider_kind="openai_compat",
        purpose="embedding",
        display_name="OpenAI text-embedding-3-large",
        vector_dim=3072,
        context_window=8191,
        vendor="openai",
    ),
    # Alibaba DashScope hosted Qwen embedding.
    ModelSpec(
        model_id="text-embedding-v3",
        provider_kind="openai_compat",
        purpose="embedding",
        display_name="通义 text-embedding-v3 (DashScope)",
        vector_dim=1024,
        context_window=8192,
        notes="Base URL: https://dashscope.aliyuncs.com/compatible-mode/v1",
        vendor="dashscope",
    ),
)


# ---------------------------------------------------------------------------
# Image embedding models — images and text into one shared vector space, so a
# picture or a sentence can both be used to find pictures. Kept apart from the
# text embeddings above: their vectors are a different space and size, and are
# stored in their own collection.
# ---------------------------------------------------------------------------

_IMAGE_EMBEDDING_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        model_id="siglip2-base",
        provider_kind="local_image_embedding",
        purpose="image_embedding",
        display_name="Google SigLIP2 base (local, multilingual)",
        vector_dim=768,
        context_window=64,
        notes="Apache-2.0 licence, ~375M parameters. Understands Chinese, English and "
        "~100 other languages. Plain transformers + safetensors from a pinned revision; "
        "no code from the model repository runs. The picture half (~0.35 GB) and the "
        "text half (~0.7 GB) load separately, only when needed.",
    ),
    ModelSpec(
        model_id="bge-vl-base",
        provider_kind="local_image_embedding",
        purpose="image_embedding",
        display_name="BAAI BGE-VL base (local, CLIP-style, English only)",
        vector_dim=512,
        context_window=77,
        notes="MIT licence, ~150M parameters, ~0.5 GB in memory. Loaded from a "
        "pinned revision; its remote code is hash-checked before it runs.",
    ),
)


# ---------------------------------------------------------------------------
# Rerankers — cross-encoders that score (query, chunk) pairs jointly.
# ---------------------------------------------------------------------------

_RERANKER_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        model_id="bge-reranker-v2-m3",
        provider_kind="local_reranker",
        purpose="reranker",
        display_name="BAAI BGE reranker v2 (local, m3)",
        context_window=8192,
        notes="Cross-encoder that reorders top-K retrieval. ~570 MB weights.",
    ),
)


MODEL_REGISTRY: dict[str, ModelSpec] = {
    m.model_id: m
    for m in (*_CHAT_MODELS, *_EMBEDDING_MODELS, *_IMAGE_EMBEDDING_MODELS, *_RERANKER_MODELS)
}


# Human-readable vendor names for the settings UI / platform admin console —
# keyed by the same short ``vendor`` string stored on ``ModelSpec`` /
# ``AiProviderBinding.vendor``. A vendor missing from this map (e.g. a
# brand-new one an admin just wired up) falls back to showing the raw key.
VENDOR_DISPLAY_NAMES: dict[str, str] = {
    "deepseek": "DeepSeek",
    "openai": "OpenAI",
    "dashscope": "通义千问 · DashScope",
    "moonshot": "Kimi · Moonshot",
    "zhipu": "智谱 · GLM",
}


def vendor_display_name(vendor: Optional[str]) -> str:
    if not vendor:
        return ""
    return VENDOR_DISPLAY_NAMES.get(vendor, vendor)


def capabilities_for(model_id: str) -> ModelSpec:
    """Look up a model spec, raising ``KeyError`` if unknown."""

    return MODEL_REGISTRY[model_id]


def models_for_purpose(purpose: str) -> list[ModelSpec]:
    if purpose == "vision":
        # A vision model is a chat model that can read pictures, not a separate kind.
        return [m for m in MODEL_REGISTRY.values() if m.supports_vision]
    return [m for m in MODEL_REGISTRY.values() if m.purpose == purpose]


def models_for_vendor(vendor: str) -> list[ModelSpec]:
    """Every registered model served by ``vendor`` (any purpose)."""

    return [m for m in MODEL_REGISTRY.values() if m.vendor == vendor]
