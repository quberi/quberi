"""AI provider abstraction — one façade over every LLM and embedding vendor.

Public entry points:

* :func:`resolve_chat_provider` / :func:`resolve_embedding_provider` —
  given an org id, hand back a ready-to-call provider instance for the
  requested purpose. Uses the org's :class:`AiModelSelection` first,
  falls back to the platform-default binding.
* :class:`ChatProvider` / :class:`EmbeddingProvider` — the two ABCs
  every implementation must satisfy. Callers depend only on these; the
  concrete provider (DeepSeek, OpenAI, Anthropic, DashScope, local
  BGE-M3, …) is hidden behind :func:`resolve_*`.
* :data:`MODEL_REGISTRY` — canonical catalog of "which model has what
  capabilities" (vector_dim, context window, supported purposes). New
  models are added by editing :mod:`registry.py`, not the database.
"""

from quberi_api.services.ai.base import (
    ChatMessage,
    ChatProvider,
    ChatResponse,
    ChatStreamChunk,
    EmbeddingProvider,
    ProviderConfig,
)
from quberi_api.services.ai.registry import MODEL_REGISTRY, ModelSpec, capabilities_for
from quberi_api.services.ai.resolver import (
    ProviderResolutionError,
    resolve_chat_provider,
    resolve_chat_provider_explicit,
    resolve_embedding_provider,
)

__all__ = [
    "ChatMessage",
    "ChatProvider",
    "ChatResponse",
    "ChatStreamChunk",
    "EmbeddingProvider",
    "MODEL_REGISTRY",
    "ModelSpec",
    "ProviderConfig",
    "ProviderResolutionError",
    "capabilities_for",
    "resolve_chat_provider",
    "resolve_chat_provider_explicit",
    "resolve_embedding_provider",
]
