"""Tool ABC, execution context, and registry.

A *tool* is the smallest reusable unit an agent can invoke. It wraps an
existing service call (never new business logic — tools are thin,
permission-preserving adapters) and exposes three things the model needs:

* ``name`` — the function name the model calls.
* ``description`` — what it does and *when to use it* (the model reads
  this to plan; write it for the model, not for humans).
* ``parameters`` — a JSON Schema object describing the arguments.

and one thing the runner needs:

* ``run(ctx, args)`` — execute against ``ctx`` (session + scoped identity)
  and return a **JSON-serialisable** result. Whatever comes back is fed to
  the model verbatim as the tool observation, so keep it compact and
  self-describing (ids + human labels, not raw ORM rows).

Permission model
----------------
Tools receive a :class:`ToolContext` carrying ``organization_id`` and
``user_id``. They MUST pass ``user_id`` down to the service layer so the
same access checks that guard the REST API also guard the agent. A tool
that reads org-wide, non-sensitive structure (the org chart, the position
list) may skip a per-resource check, but anything space-scoped
(documents, knowledge retrieval, conversations) must go through the
access-gated service path. The agent can never out-reach its caller.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, List, Optional

from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncSession


class ToolError(Exception):
    """Raised by a tool when it cannot complete a call.

    The runner catches this and feeds the message back to the model as
    the tool observation (rather than aborting the whole run), so the
    model can recover — re-try with different arguments, or route around
    the failure. Use it for *expected* failure modes (not found, bad
    argument); let genuinely unexpected exceptions propagate.
    """


@dataclass
class ToolContext:
    """Everything a tool needs to execute, scoped to the calling user.

    ``qdrant`` is lazily present: only knowledge-retrieval tools need it,
    and the runner owns its lifecycle (opens once per run, closes at the
    end), so tools should treat it as "may be None" and raise
    :class:`ToolError` if they require it and it's absent.
    """

    session: AsyncSession
    organization_id: uuid.UUID
    user_id: uuid.UUID
    qdrant: Optional[AsyncQdrantClient] = None
    # Knowledge scope inherited from the running agent's config. Consumed by
    # the knowledge-search tool: "user" follows the caller's access,
    # "intersect" narrows to configured spaces ∩ caller access, "fixed" uses
    # the configured spaces regardless of caller access.
    knowledge_scope_mode: str = "user"
    knowledge_space_ids: Optional[List[uuid.UUID]] = None

    def require_qdrant(self) -> AsyncQdrantClient:
        if self.qdrant is None:
            raise ToolError("vector store is not available for this run")
        return self.qdrant


class Tool(ABC):
    """One callable capability exposed to an agent."""

    #: Function name the model calls. Must match ``^[a-zA-Z0-9_]+$`` per
    #: the OpenAI function-calling spec.
    name: str = ""

    #: Natural-language description the model uses to decide when to call.
    description: str = ""

    #: JSON Schema (an ``object``) describing the accepted arguments.
    #: Default is "no arguments"; override for tools that take input.
    parameters: dict = {"type": "object", "properties": {}}

    @abstractmethod
    async def run(self, ctx: ToolContext, args: dict) -> Any:
        """Execute the tool and return a JSON-serialisable observation."""

    def to_openai_schema(self) -> dict:
        """Render this tool as an OpenAI ``tools`` array entry."""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description.strip(),
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """An ordered catalogue of tools available to one agent run.

    Kept per-run (rather than a global singleton) so a future
    per-org/per-agent config can hand the runner a *subset* of tools
    without mutating shared state.
    """

    def __init__(self, tools: Optional[list[Tool]] = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError(f"tool {tool!r} has no name")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name!r}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def openai_schemas(self) -> list[dict]:
        """The ``tools`` array to hand the chat provider."""

        return [t.to_openai_schema() for t in self._tools.values()]

    def __len__(self) -> int:  # pragma: no cover — trivial
        return len(self._tools)
