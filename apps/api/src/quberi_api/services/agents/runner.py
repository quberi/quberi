"""Agent runner — the plan→call→observe loop.

Drives a chat model that can call tools. Each iteration:

1. Ask the model to ``complete`` given the running message history + the
   tool catalogue.
2. If the model returned an *answer* (no ``tool_calls``) → we're done.
3. If it returned ``tool_calls`` → execute each one against the scoped
   :class:`ToolContext`, append the assistant turn (carrying the exact
   ``tool_calls``) plus one ``role='tool'`` observation per call, and loop.

We cap the loop at ``MAX_ITERATIONS`` tool rounds. If the model still
wants tools after the cap, we make one final ``complete`` call **without**
a tool catalogue so it's forced to answer from what it already gathered
(rather than spinning forever).

Everything the model requested and observed is captured in a ``trace`` so
the UI can render *how* the answer was reached — the whole point of the
agent surface is showing the reasoning across sources, not just the
final sentence.

Session & Qdrant lifecycle
--------------------------
Tools run read-only SELECTs, which auto-begin a read transaction on the
async session. Between iterations we ``commit`` to release the connection
before the next (network-bound) model call — the same pattern the
conversation service uses. The runner owns the Qdrant client: it opens
one per run and closes it in a ``finally``.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.core.config import get_settings
from quberi_api.domain.agents.models import Agent
from quberi_api.infrastructure.qdrant import create_qdrant_client
from quberi_api.services.agents import config as agents_config
from quberi_api.services.agents.base import Tool, ToolContext, ToolError, ToolRegistry
from quberi_api.services.agents.tools import build_default_tools, build_tools_by_names
from quberi_api.services.ai import resolve_chat_provider
from quberi_api.services.ai.base import ChatMessage, ChatProvider
from quberi_api.services.errors import NotFoundError

_log = logging.getLogger(__name__)

MAX_ITERATIONS = 6

# Observation payloads are fed back to the model as tokens; cap the JSON
# size so one over-eager tool result can't blow the context budget.
_MAX_OBSERVATION_CHARS = 12_000


SYSTEM_PROMPT = """You are Quberi's org assistant. You help members of a company \
get answers by reasoning across two kinds of information:

1. Unstructured knowledge — documents, wikis, meeting notes, synced files — \
searchable with the search_knowledge tool.
2. Structured organization data — people, departments, positions, knowledge \
spaces, and access rights — via the org tools (list_members, get_member_profile, \
list_spaces, get_space_access, list_org_units, list_org_unit_members, \
list_positions).

How to work:
- Break the question into steps and call tools to gather facts. Prefer calling \
a tool over guessing. You may call several tools, in sequence, before answering.
- Combine results across sources when it helps (e.g. count people in a \
department AND find which knowledge spaces they own).
- Every tool is scoped to what the current user is allowed to see; if a tool \
returns nothing or an error, say so plainly rather than inventing an answer.
- When you cite knowledge passages, refer to them naturally; don't fabricate \
document names or numbers.
- Answer in the same language the user asked in. Be concise and specific.

When you have enough information, stop calling tools and give the final answer."""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class TraceStep:
    """One step in the agent's reasoning trace."""

    kind: str  # "tool_call" | "tool_result" | "answer" | "note"
    data: dict

    def to_dict(self) -> dict:
        return {"kind": self.kind, **self.data}


@dataclass
class AgentRunResult:
    answer: str
    trace: list[TraceStep] = field(default_factory=list)
    iterations: int = 0
    model_id: str = ""
    tool_call_count: int = 0

    def to_dict(self) -> dict:
        return {
            "answer": self.answer,
            "iterations": self.iterations,
            "model_id": self.model_id,
            "tool_call_count": self.tool_call_count,
            "trace": [s.to_dict() for s in self.trace],
        }


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _default_registry() -> ToolRegistry:
    return ToolRegistry(build_default_tools())


async def _resolve_agent(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    agent_id: Optional[uuid.UUID],
) -> Agent:
    """Load the requested agent, or the org's default when unspecified."""

    if agent_id is not None:
        return await agents_config.get_agent(
            session, organization_id=organization_id, agent_id=agent_id
        )
    return await agents_config.get_default_agent(
        session, organization_id=organization_id
    )


def _registry_for(agent: Agent) -> ToolRegistry:
    return ToolRegistry(build_tools_by_names(list(agent.enabled_tools or [])))


def _scope_space_uuids(agent: Agent) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for sid in agent.scope_space_ids or []:
        try:
            out.append(uuid.UUID(str(sid)))
        except (ValueError, TypeError):
            continue
    return out


def _system_prompt_for(agent: Agent) -> str:
    """Base runner prompt + the agent's persona addendum (if any)."""

    extra = (agent.system_prompt or "").strip()
    if not extra:
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\n# This agent's focus\n{extra}"


def _clip(text: str, limit: int = _MAX_OBSERVATION_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


async def _execute_tool(
    registry: ToolRegistry, ctx: ToolContext, name: str, args: dict
) -> dict:
    """Run one tool call, converting expected failures to an error payload.

    Returns a JSON-serialisable observation dict either way — the model
    reads both success and error observations, so a bad call becomes a
    recoverable signal rather than a crash.
    """

    tool: Optional[Tool] = registry.get(name)
    if tool is None:
        return {"error": f"unknown tool: {name}"}
    try:
        result = await tool.run(ctx, args or {})
        return result if isinstance(result, dict) else {"result": result}
    except ToolError as exc:
        return {"error": str(exc)}
    except NotFoundError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — surface to model, log for us
        _log.exception("tool %s crashed", name)
        return {"error": f"{type(exc).__name__}: {exc}"}


def _observation_message(call_id: str, name: str, observation: dict) -> ChatMessage:
    payload = _clip(json.dumps(observation, ensure_ascii=False, default=str))
    return ChatMessage(
        role="tool",
        content=payload,
        name=name,
        tool_call_id=call_id,
    )


# ---------------------------------------------------------------------------
# Non-streaming
# ---------------------------------------------------------------------------


async def run_agent(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    query: str,
    agent_id: Optional[uuid.UUID] = None,
    history: Optional[list[ChatMessage]] = None,
    registry: Optional[ToolRegistry] = None,
    max_iterations: int = MAX_ITERATIONS,
) -> AgentRunResult:
    """Run the agent to completion and return the answer + full trace.

    ``agent_id`` selects a configured persona (its tool subset + system
    prompt). When omitted, the org's default generalist is used. An
    explicit ``registry`` overrides the agent's tool set (used by tests).
    """

    query = (query or "").strip()
    if not query:
        raise ValueError("query is required")

    agent = await _resolve_agent(
        session, organization_id=organization_id, agent_id=agent_id
    )
    registry = registry or _registry_for(agent)
    system_prompt = _system_prompt_for(agent)
    resolved = await resolve_chat_provider(session, organization_id=organization_id)
    provider: ChatProvider = resolved.provider  # type: ignore[assignment]
    model_id = resolved.model.model_id
    tools_schema = registry.openai_schemas()
    # Release the read txn opened by the agent + provider lookups before
    # the network-bound loop begins.
    await session.commit()

    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=system_prompt),
        *(history or []),
        ChatMessage(role="user", content=query),
    ]
    trace: list[TraceStep] = []
    tool_call_count = 0

    qdrant = create_qdrant_client(get_settings())
    ctx = ToolContext(
        session=session,
        organization_id=organization_id,
        user_id=user_id,
        qdrant=qdrant,
        knowledge_scope_mode=agent.knowledge_scope_mode,
        knowledge_space_ids=_scope_space_uuids(agent),
    )
    try:
        iterations = 0
        answer = ""
        for iterations in range(1, max_iterations + 1):
            response = await provider.complete(
                messages=messages,
                model=model_id,
                tools=tools_schema,
            )
            if not response.tool_calls:
                answer = response.content or ""
                break

            # Record the assistant's tool-calling turn verbatim so the
            # next model call sees its own prior decisions.
            messages.append(
                ChatMessage(
                    role="assistant",
                    content=response.content or "",
                    tool_calls=response.tool_calls,
                )
            )
            if response.content:
                trace.append(TraceStep("note", {"text": response.content}))

            for tc in response.tool_calls:
                tool_call_count += 1
                trace.append(
                    TraceStep("tool_call", {"tool": tc.name, "arguments": tc.arguments})
                )
                observation = await _execute_tool(registry, ctx, tc.name, tc.arguments)
                trace.append(
                    TraceStep("tool_result", {"tool": tc.name, "result": observation})
                )
                messages.append(_observation_message(tc.id, tc.name, observation))

            # Release the read transaction the tool SELECTs opened before
            # the next network-bound model call.
            await session.commit()
        else:
            # Loop exhausted while the model still wanted tools — force a
            # final answer without the tool catalogue.
            _log.info("agent hit iteration cap (%d); forcing answer", max_iterations)
            final = await provider.complete(messages=messages, model=model_id)
            answer = final.content or ""
            trace.append(
                TraceStep("note", {"text": "(reached tool-call limit; answering now)"})
            )

        trace.append(TraceStep("answer", {"text": answer}))
        return AgentRunResult(
            answer=answer,
            trace=trace,
            iterations=iterations,
            model_id=model_id,
            tool_call_count=tool_call_count,
        )
    finally:
        await qdrant.close()


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


async def run_agent_stream(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    query: str,
    agent_id: Optional[uuid.UUID] = None,
    history: Optional[list[ChatMessage]] = None,
    registry: Optional[ToolRegistry] = None,
    max_iterations: int = MAX_ITERATIONS,
) -> AsyncIterator[tuple[str, dict]]:
    """Streaming twin of :func:`run_agent`.

    Yields ``(kind, data)`` tuples ready for SSE framing:

        ('tool_call',   {tool, arguments})
        ('tool_result', {tool, result})
        ('note',        {text})              # model's between-step reasoning
        ('delta',       {text})              # final-answer chunks
        ('done',        {iterations, tool_call_count, model_id})
        ('error',       {message})           # on failure

    The tool-calling rounds use non-streaming ``complete`` (we need the
    full ``tool_calls`` array to act on); the final answer is streamed
    token-by-token via the provider's ``stream`` once no more tools are
    requested.
    """

    query = (query or "").strip()
    if not query:
        yield "error", {"message": "query is required"}
        yield "done", {}
        return

    qdrant = create_qdrant_client(get_settings())
    ctx = ToolContext(
        session=session,
        organization_id=organization_id,
        user_id=user_id,
        qdrant=qdrant,
    )
    try:
        agent = await _resolve_agent(
            session, organization_id=organization_id, agent_id=agent_id
        )
        registry = registry or _registry_for(agent)
        system_prompt = _system_prompt_for(agent)
        # Now that the agent is known, apply its knowledge scope to the ctx.
        ctx.knowledge_scope_mode = agent.knowledge_scope_mode
        ctx.knowledge_space_ids = _scope_space_uuids(agent)
        resolved = await resolve_chat_provider(
            session, organization_id=organization_id
        )
        provider: ChatProvider = resolved.provider  # type: ignore[assignment]
        model_id = resolved.model.model_id
        tools_schema = registry.openai_schemas()
        await session.commit()

        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=system_prompt),
            *(history or []),
            ChatMessage(role="user", content=query),
        ]
        tool_call_count = 0
        iterations = 0
        needs_forced_answer = False

        while True:
            iterations += 1
            if iterations > max_iterations:
                needs_forced_answer = True
                break

            response = await provider.complete(
                messages=messages,
                model=model_id,
                tools=tools_schema,
            )
            if not response.tool_calls:
                # Model answered directly. Stream that answer for a live
                # feel by re-running the final turn without tools.
                messages.append(
                    ChatMessage(role="assistant", content=response.content or "")
                )
                break

            messages.append(
                ChatMessage(
                    role="assistant",
                    content=response.content or "",
                    tool_calls=response.tool_calls,
                )
            )
            if response.content:
                yield "note", {"text": response.content}

            for tc in response.tool_calls:
                tool_call_count += 1
                yield "tool_call", {"tool": tc.name, "arguments": tc.arguments}
                observation = await _execute_tool(registry, ctx, tc.name, tc.arguments)
                yield "tool_result", {"tool": tc.name, "result": observation}
                messages.append(_observation_message(tc.id, tc.name, observation))

            await session.commit()

        # ---- Stream the final answer ----
        # Drop the placeholder assistant turn we may have appended (from the
        # direct-answer branch); we re-generate it as a stream so the client
        # sees tokens. Feeding the same history without tools yields the
        # same answer, now streamed.
        if messages and messages[-1].role == "assistant" and not messages[-1].tool_calls:
            messages.pop()

        streamed_any = False
        async for chunk in provider.stream(messages=messages, model=model_id):
            if chunk.delta:
                streamed_any = True
                yield "delta", {"text": chunk.delta}

        if not streamed_any and needs_forced_answer:
            # Extremely defensive: stream produced nothing; fall back to a
            # blocking completion so the user still gets words.
            final = await provider.complete(messages=messages, model=model_id)
            if final.content:
                yield "delta", {"text": final.content}

        yield "done", {
            "iterations": iterations,
            "tool_call_count": tool_call_count,
            "model_id": model_id,
        }
    except Exception as exc:  # noqa: BLE001 — surface as SSE error
        _log.exception("agent stream failed")
        yield "error", {"message": f"{type(exc).__name__}: {exc}"}
        yield "done", {}
    finally:
        await qdrant.close()
