"""Provider ABCs and shared value types.

Every LLM/embedding vendor is invisible to the rest of the codebase —
callers depend only on :class:`ChatProvider` / :class:`EmbeddingProvider`
and the small dataclasses they consume. Adding XAI, Cohere, or a
self-hosted vLLM instance means writing one file that implements these
protocols, not touching business logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, List, Optional, Sequence


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderConfig:
    """The subset of an :class:`AiProviderBinding` a provider actually needs.

    Kept intentionally small so the resolver can hand off a value type
    rather than the ORM row (avoids lifetime and thread coupling with
    the request session).
    """

    kind: str  # "openai_compat" / "openai" / "anthropic" / "dashscope" / "local_embedding" / ...
    display_name: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None  # already decrypted
    extra: dict = field(default_factory=dict)  # provider-specific knobs


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation the model requested (OpenAI function-calling)."""

    id: str
    name: str
    arguments: dict  # already JSON-parsed


@dataclass(frozen=True)
class ImagePart:
    """One picture sent with a user message to a model that can read pictures."""

    mime_type: str  # "image/jpeg" | "image/png" | "image/gif" | "image/webp"
    data: bytes


@dataclass(frozen=True)
class ChatMessage:
    """One turn in a chat conversation. Roles follow the OpenAI schema.

    For assistant turns that requested tools, ``tool_calls`` carries the
    requested calls (so we can replay the exact assistant message back
    to the model on the next loop iteration). For ``role='tool'`` turns,
    ``tool_call_id`` links the result to its request.
    """

    role: str  # "system" / "user" / "assistant" / "tool"
    content: str
    name: Optional[str] = None  # for tool messages
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List["ToolCall"]] = None
    # Pictures that go with ``content``. Only a *user* message may carry them (providers
    # answer 400 for a picture in a system or assistant message), and only a model that can
    # read pictures should be given any. Kept apart from ``content`` so every place that
    # reads ``content`` as text keeps working.
    images: tuple[ImagePart, ...] = ()


@dataclass(frozen=True)
class ChatResponse:
    """Non-streaming chat completion result.

    ``tool_calls`` is populated when the model chose to call tools
    instead of (or before) answering; ``content`` may be empty in that
    case. The agent runner loops until a response arrives with no
    ``tool_calls``.
    """

    content: str
    finish_reason: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: dict = field(default_factory=dict)
    tool_calls: List["ToolCall"] = field(default_factory=list)


@dataclass(frozen=True)
class ChatStreamChunk:
    """One SSE delta from a streaming chat completion.

    ``delta`` is the incremental content since the last chunk (already
    concatenatable, no per-chunk metadata to strip). ``finish_reason`` is
    only populated on the final chunk.
    """

    delta: str
    finish_reason: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


# ---------------------------------------------------------------------------
# Abstract providers
# ---------------------------------------------------------------------------


class ChatProvider(ABC):
    """LLM chat completions — sync ``complete`` + streaming ``stream``."""

    #: The provider_kind string this class serves (used by the registry).
    kind: str = ""

    @abstractmethod
    async def complete(
        self,
        *,
        messages: Sequence[ChatMessage],
        model: str,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        extra: Optional[dict] = None,
        tools: Optional[List[dict]] = None,
    ) -> ChatResponse:
        """Run a non-streaming chat completion.

        ``tools`` is a list of OpenAI-schema tool definitions
        (``{"type": "function", "function": {...}}``). When provided the
        model may return ``tool_calls`` on the response instead of a
        final answer.
        """

    @abstractmethod
    async def stream(
        self,
        *,
        messages: Sequence[ChatMessage],
        model: str,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        extra: Optional[dict] = None,
    ) -> AsyncIterator[ChatStreamChunk]:
        """Stream a chat completion as ``ChatStreamChunk``s."""


class EmbeddingProvider(ABC):
    """Text → fixed-length vectors."""

    kind: str = ""

    @property
    @abstractmethod
    def vector_dim(self) -> int:
        """Dimensionality of the vectors returned by :meth:`embed`.

        Must be stable across a process lifetime; the Qdrant collection
        pinned to this provider assumes it.
        """

    @abstractmethod
    async def embed(
        self,
        texts: Sequence[str],
        *,
        model: str,
        instruction: Optional[str] = None,
    ) -> List[List[float]]:
        """Embed a batch of strings.

        ``instruction`` is used by instruction-aware models
        (Qwen3-Embedding is a prime example) and is silently ignored by
        providers that don't consume it.
        """
