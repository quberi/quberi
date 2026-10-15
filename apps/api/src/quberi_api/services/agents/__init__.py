"""Agent / tool-calling layer.

The 跃迁 ("phase transition") from single-shot RAG Q&A to an agent that
*reasons* across multiple sources: it can chain a knowledge search with a
lookup against structured org data (members / positions / org units /
spaces) to answer questions no single retrieval could — e.g. "how many
people are in Engineering and which knowledge spaces do they own?".

Three layers, deliberately separated:

* **Tool** (:mod:`quberi_api.services.agents.base`) — a unit of *code*:
  a name, a description, a JSON-schema parameter spec, and an async
  ``run(ctx, args)``. Every tool is permission-gated: it calls the same
  services the REST API does, scoped to ``ctx.user_id``, so an agent can
  never see more than the calling user could see by hand.
* **Registry** — the catalogue of tools available to an agent, plus the
  OpenAI-schema serialisation handed to the model.
* **Runner** (:mod:`quberi_api.services.agents.runner`) — the
  plan→call→observe loop that drives the model, executes the tool calls
  it requests, feeds the observations back, and repeats until the model
  produces a final answer (or we hit the iteration ceiling).
"""

from __future__ import annotations

from quberi_api.services.agents.base import (
    Tool,
    ToolContext,
    ToolError,
    ToolRegistry,
)

__all__ = [
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
]
