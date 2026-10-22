"""OpenAI-schema-compatible provider.

Single implementation that handles OpenAI itself, DeepSeek, Kimi/Moonshot,
DashScope's compat mode, Zhipu, MiniMax, self-hosted vLLM/Ollama — any
endpoint that speaks the OpenAI ``/chat/completions`` and ``/embeddings``
routes. What varies is ``base_url``, the API key header, and the model
name; the rest is identical.

Streaming
---------
We consume Server-Sent Events line by line via ``httpx.AsyncClient.stream``,
extract the ``data:`` payloads, and parse each JSON chunk. ``data: [DONE]``
ends the stream. This avoids pulling in ``openai`` as a dep; the wire
format is stable and simple enough.
"""

from __future__ import annotations

import base64
import json
from typing import AsyncIterator, List, Optional, Sequence

import httpx

from quberi_api.services.ai.base import (
    ChatMessage,
    ChatProvider,
    ChatResponse,
    ChatStreamChunk,
    EmbeddingProvider,
    ProviderConfig,
    ToolCall,
)
from quberi_api.services.ai.registry import MODEL_REGISTRY


DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0, read=120.0)


class OpenAICompatProvider(ChatProvider, EmbeddingProvider):
    """One provider serving both chat and embedding purposes.

    Whether ``embed`` or ``complete`` is called depends on the caller; we
    don't gate at the provider level because a single OpenAI key legally
    unlocks both endpoints. The resolver picks whichever role fits the
    ``ai_model_selections`` purpose.
    """

    kind = "openai_compat"

    def __init__(self, config: ProviderConfig, *, vector_dim: int = 1536) -> None:
        if not config.base_url:
            raise ValueError(
                f"OpenAICompatProvider ({config.display_name}) requires base_url"
            )
        self._config = config
        # Trim any trailing slash so we can concat with ``/chat/completions``
        # etc. without doubling up.
        self._base_url = config.base_url.rstrip("/")
        self._vector_dim = vector_dim

    # ------------------------------------------------------------------
    # EmbeddingProvider
    # ------------------------------------------------------------------

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
        # OpenAI-schema embeddings ignore the ``instruction`` argument; a
        # provider that consumes it (e.g. Qwen local) overrides this class.
        del instruction
        payload = {"model": model, "input": list(texts)}
        headers = self._headers()
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base_url}/embeddings",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            body = resp.json()
        vectors = [row["embedding"] for row in body["data"]]
        # Enforce the declared dim so a config typo (e.g. selecting a
        # 3072-d model against a 1536-d collection) blows up loudly at
        # provider boundary, not silently at Qdrant.
        if vectors and len(vectors[0]) != self._vector_dim:
            raise RuntimeError(
                f"provider {self._config.display_name} returned "
                f"{len(vectors[0])}-d vectors, expected {self._vector_dim}-d"
            )
        return vectors

    # ------------------------------------------------------------------
    # Model discovery
    # ------------------------------------------------------------------

    async def list_models(self) -> List[str]:
        """Fetch available model ids via the OpenAI-compatible ``/models``.

        Returns the ``data[].id`` strings. Not every compatible endpoint
        implements this route — callers must handle an HTTP error or an
        empty list by falling back to manual entry.
        """

        timeout = httpx.Timeout(15.0, connect=8.0, read=15.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{self._base_url}/models", headers=self._headers())
            resp.raise_for_status()
            body = resp.json()
        data = body.get("data") if isinstance(body, dict) else None
        ids: List[str] = []
        if isinstance(data, list):
            for row in data:
                mid = (row or {}).get("id") if isinstance(row, dict) else None
                if mid:
                    ids.append(str(mid))
        return sorted(set(ids))

    # ------------------------------------------------------------------
    # ChatProvider
    # ------------------------------------------------------------------

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
        payload = self._chat_payload(
            messages, model, temperature, max_tokens, extra, stream=False
        )
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
            )
            resp.raise_for_status()
            body = resp.json()
        choice = body["choices"][0]
        message = choice.get("message") or {}
        usage = body.get("usage") or {}
        return ChatResponse(
            content=message.get("content", "") or "",
            finish_reason=choice.get("finish_reason"),
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            raw=body,
            tool_calls=_parse_tool_calls(message.get("tool_calls")),
        )

    async def stream(
        self,
        *,
        messages: Sequence[ChatMessage],
        model: str,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        extra: Optional[dict] = None,
    ) -> AsyncIterator[ChatStreamChunk]:
        payload = self._chat_payload(
            messages, model, temperature, max_tokens, extra, stream=True
        )
        headers = self._headers()
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    # SSE frames arrive one per line; blanks are frame
                    # separators. Ignore anything not prefixed ``data: ``.
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        return
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        # Some providers emit keepalive comments; skip
                        # rather than tear down the whole stream.
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    ch = choices[0]
                    delta = (ch.get("delta") or {}).get("content", "") or ""
                    finish = ch.get("finish_reason")
                    usage = chunk.get("usage") or {}
                    if not delta and not finish:
                        continue
                    yield ChatStreamChunk(
                        delta=delta,
                        finish_reason=finish,
                        prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                        completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        # A few providers want extra tenant / workspace ids in headers.
        for k, v in (self._config.extra or {}).items():
            if k.startswith("header:") and isinstance(v, str):
                headers[k[len("header:"):]] = v
        return headers

    def _chat_payload(
        self,
        messages: Sequence[ChatMessage],
        model: str,
        temperature: float,
        max_tokens: Optional[int],
        extra: Optional[dict],
        *,
        stream: bool,
    ) -> dict:
        # Some models (e.g. Kimi K2.x) reject every temperature but one.
        spec = MODEL_REGISTRY.get(model)
        if spec is not None and spec.fixed_temperature is not None:
            temperature = spec.fixed_temperature
        payload: dict = {
            "model": model,
            "temperature": temperature,
            "messages": [self._serialize_message(m) for m in messages],
            "stream": stream,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if stream:
            # DeepSeek + OpenAI both return usage in the *last* stream
            # chunk when this option is set. Older OSS forks may ignore
            # it, which is harmless.
            payload["stream_options"] = {"include_usage": True}
        if extra:
            payload.update(extra)
        return payload

    @staticmethod
    def _serialize_message(m: ChatMessage) -> dict:
        """Render one :class:`ChatMessage` into the OpenAI wire schema.

        Assistant turns that requested tools must replay ``tool_calls``
        verbatim (id + function name + JSON-string arguments) so the
        model keeps its thread; ``role='tool'`` turns carry the result
        keyed by ``tool_call_id``. OpenAI rejects a ``null`` content on
        an assistant message that has ``tool_calls``, so we coerce to an
        empty string only when there's no textual content.
        """

        out: dict = {"role": m.role}
        if m.images:
            if m.role != "user":
                raise ValueError("a picture may only be sent in a user message")
            out["content"] = [
                {"type": "text", "text": m.content},
                *(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{p.mime_type};base64,"
                            + base64.b64encode(p.data).decode("ascii")
                        },
                    }
                    for p in m.images
                ),
            ]
            return out
        if m.tool_calls:
            out["content"] = m.content or ""
            out["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in m.tool_calls
            ]
        else:
            out["content"] = m.content
        if m.name is not None:
            out["name"] = m.name
        if m.tool_call_id is not None:
            out["tool_call_id"] = m.tool_call_id
        return out


def _parse_tool_calls(raw: Optional[list]) -> List[ToolCall]:
    """Parse the ``message.tool_calls`` array from a completion response.

    Each entry looks like
    ``{"id": "call_x", "type": "function",
       "function": {"name": "...", "arguments": "<json string>"}}``.
    Arguments arrive as a JSON *string*; we parse it to a dict, tolerating
    the empty-string / malformed case by falling back to ``{}`` rather
    than blowing up the whole loop.
    """

    if not raw:
        return []
    calls: List[ToolCall] = []
    for entry in raw:
        fn = (entry or {}).get("function") or {}
        name = fn.get("name") or ""
        if not name:
            continue
        raw_args = fn.get("arguments")
        if isinstance(raw_args, dict):
            args = raw_args
        else:
            try:
                args = json.loads(raw_args) if raw_args else {}
            except (json.JSONDecodeError, TypeError):
                args = {}
        calls.append(
            ToolCall(
                id=entry.get("id") or f"call_{len(calls)}",
                name=name,
                arguments=args if isinstance(args, dict) else {},
            )
        )
    return calls
