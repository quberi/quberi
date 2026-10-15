"""External agent caller.

An ``kind='external'`` agent forwards the conversation to a third-party
endpoint instead of running our in-house tool loop. Supported protocols
(``external_config.protocol``):

* ``openai_chat`` — any OpenAI ``/chat/completions`` endpoint (Dify compat
  layer, FastGPT, OneAPI, vLLM, self-hosted models, …). Reuses
  :class:`OpenAICompatProvider`.
* ``coze`` — 扣子 Coze ``/v3/chat``. Needs a ``bot_id``; we pass our history
  as ``additional_messages`` so multi-turn works statelessly.
* ``dify`` — Dify ``/chat-messages`` (app key). First version is single-turn
  (Dify tracks context via its own ``conversation_id``, which we don't yet
  persist).

Both entry points mirror the builtin runner's shapes so the conversation
service can route to either without special-casing the result:

* :func:`run_external_agent`        → :class:`AgentRunResult`
* :func:`run_external_agent_stream` → yields ``(kind, data)`` SSE tuples
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import AsyncIterator, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.agents.models import Agent
from quberi_api.services.agents.runner import AgentRunResult
from quberi_api.services.ai.base import ChatMessage, ProviderConfig
from quberi_api.services.ai.openai_compat import OpenAICompatProvider
from quberi_api.services.security.crypto import unseal

_log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(120.0, connect=10.0, read=120.0)


def _key(agent: Agent) -> Optional[str]:
    return (
        unseal(agent.external_api_key_encrypted)
        if agent.external_api_key_encrypted
        else None
    )


def _history_messages(
    agent: Agent, query: str, history: Optional[list[ChatMessage]]
) -> list[ChatMessage]:
    msgs: list[ChatMessage] = []
    prompt = (agent.system_prompt or "").strip()
    if prompt:
        msgs.append(ChatMessage(role="system", content=prompt))
    msgs.extend(history or [])
    msgs.append(ChatMessage(role="user", content=query))
    return msgs


# ---------------------------------------------------------------------------
# openai_chat
# ---------------------------------------------------------------------------


async def _openai_stream(
    agent: Agent, query: str, history: Optional[list[ChatMessage]]
) -> AsyncIterator[tuple[str, dict]]:
    cfg = agent.external_config or {}
    endpoint, model = cfg.get("endpoint"), cfg.get("model")
    if not endpoint or not model:
        yield "error", {"message": "external agent is missing endpoint or model"}
        yield "done", {}
        return
    provider = OpenAICompatProvider(
        ProviderConfig(
            kind="openai_compat",
            display_name=agent.name,
            base_url=endpoint,
            api_key=_key(agent),
            extra=cfg.get("extra") or {},
        )
    )
    try:
        async for chunk in provider.stream(
            messages=_history_messages(agent, query, history), model=str(model)
        ):
            if chunk.delta:
                yield "delta", {"text": chunk.delta}
    except Exception as exc:  # noqa: BLE001
        _log.exception("openai_chat external stream failed")
        yield "error", {"message": f"{type(exc).__name__}: {exc}"}
    yield "done", {"model_id": str(model)}


# ---------------------------------------------------------------------------
# Coze  (POST {endpoint}/v3/chat)
# ---------------------------------------------------------------------------


async def _coze_stream(
    agent: Agent,
    query: str,
    history: Optional[list[ChatMessage]],
    user_id: uuid.UUID,
) -> AsyncIterator[tuple[str, dict]]:
    cfg = agent.external_config or {}
    base = str(cfg.get("endpoint") or "https://api.coze.cn").rstrip("/")
    bot_id = cfg.get("bot_id")
    if not bot_id:
        yield "error", {"message": "Coze agent is missing bot_id"}
        yield "done", {}
        return

    additional: list[dict] = []
    for m in history or []:
        if m.role in ("user", "assistant") and m.content:
            additional.append(
                {"role": m.role, "content": m.content, "content_type": "text"}
            )
    additional.append(
        {"role": "user", "content": query, "content_type": "text", "type": "question"}
    )
    body = {
        "bot_id": str(bot_id),
        "user_id": str(user_id),
        "stream": True,
        "auto_save_history": True,
        "additional_messages": additional,
    }
    headers = {
        "Authorization": f"Bearer {_key(agent) or ''}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            async with client.stream(
                "POST", f"{base}/v3/chat", json=body, headers=headers
            ) as resp:
                resp.raise_for_status()
                event: Optional[str] = None
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("event:"):
                        event = line[len("event:"):].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    raw = line[len("data:"):].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    if event == "conversation.message.delta":
                        try:
                            d = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        # Only accumulate the assistant's answer deltas
                        # (skip follow-up suggestions / verbose payloads).
                        if d.get("type") in (None, "answer") and d.get("content"):
                            yield "delta", {"text": d["content"]}
                    elif event in ("conversation.chat.failed", "error"):
                        try:
                            d = json.loads(raw)
                        except json.JSONDecodeError:
                            d = {}
                        msg = (d.get("last_error") or {}).get("msg") or d.get(
                            "msg"
                        ) or "Coze chat failed"
                        yield "error", {"message": msg}
    except Exception as exc:  # noqa: BLE001
        _log.exception("coze external stream failed")
        yield "error", {"message": f"{type(exc).__name__}: {exc}"}
    yield "done", {"model_id": f"coze:{bot_id}"}


# ---------------------------------------------------------------------------
# Dify  (POST {base_url}/chat-messages)
# ---------------------------------------------------------------------------


async def _dify_stream(
    agent: Agent, query: str, user_id: uuid.UUID
) -> AsyncIterator[tuple[str, dict]]:
    cfg = agent.external_config or {}
    base = str(cfg.get("endpoint") or "https://api.dify.ai/v1").rstrip("/")
    body = {
        "inputs": {},
        "query": query,
        "response_mode": "streaming",
        "user": str(user_id),
        "conversation_id": "",
    }
    headers = {
        "Authorization": f"Bearer {_key(agent) or ''}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            async with client.stream(
                "POST", f"{base}/chat-messages", json=body, headers=headers
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[len("data:"):].strip()
                    if not raw:
                        continue
                    try:
                        d = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    ev = d.get("event")
                    if ev in ("message", "agent_message"):
                        ans = d.get("answer") or ""
                        if ans:
                            yield "delta", {"text": ans}
                    elif ev == "error":
                        yield "error", {
                            "message": d.get("message") or "Dify error"
                        }
    except Exception as exc:  # noqa: BLE001
        _log.exception("dify external stream failed")
        yield "error", {"message": f"{type(exc).__name__}: {exc}"}
    yield "done", {"model_id": "dify"}


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def _dispatch(
    agent: Agent,
    query: str,
    history: Optional[list[ChatMessage]],
    user_id: uuid.UUID,
) -> AsyncIterator[tuple[str, dict]]:
    proto = (agent.external_config or {}).get("protocol") or "openai_chat"
    if proto == "coze":
        return _coze_stream(agent, query, history, user_id)
    if proto == "dify":
        return _dify_stream(agent, query, user_id)
    return _openai_stream(agent, query, history)


async def run_external_agent_stream(
    session: AsyncSession,
    *,
    agent: Agent,
    user_id: uuid.UUID,
    query: str,
    history: Optional[list[ChatMessage]] = None,
) -> AsyncIterator[tuple[str, dict]]:
    """Stream the external endpoint's answer as ``(kind, data)`` tuples."""

    del session  # external agents don't touch our data layer
    async for event in _dispatch(agent, query, history, user_id):
        yield event


async def run_external_agent(
    session: AsyncSession,
    *,
    agent: Agent,
    user_id: uuid.UUID,
    query: str,
    history: Optional[list[ChatMessage]] = None,
) -> AgentRunResult:
    """Call the external endpoint and return the final answer (accumulated)."""

    del session
    answer = ""
    model_id = ""
    error: Optional[str] = None
    async for kind, data in _dispatch(agent, query, history, user_id):
        if kind == "delta":
            answer += data.get("text", "")
        elif kind == "done":
            model_id = data.get("model_id", "") or model_id
        elif kind == "error":
            error = data.get("message", "external agent error")
    if error and not answer:
        raise RuntimeError(error)
    return AgentRunResult(
        answer=answer.strip(),
        trace=[],
        iterations=1,
        model_id=model_id,
        tool_call_count=0,
    )
