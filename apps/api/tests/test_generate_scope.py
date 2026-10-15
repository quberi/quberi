"""Regression tests for the RAG generation layer's empty-scope handling.

Locks in a behaviour fix verified manually earlier in this project: when a
conversation's effective knowledge scope is empty (the user unselected
every space, not "spaces were searched but nothing matched"), the assistant
should answer directly from the model rather than returning the canned
"no supporting documents" message. The two situations are distinguishable
via the ``scope_is_empty`` flag the caller (``conversations.py``) computes
from ``len(retrieved.space_ids) == 0``.

The chat provider is monkeypatched (no live LLM call, no network) — these
tests are about the branching logic in ``generate_answer``/
``generate_answer_stream``, not about model quality.
"""

from __future__ import annotations

import types
import uuid

import pytest

from quberi_api.services.rag import generate as generate_module


class _FakeProvider:
    """A minimal stand-in for a ``ChatProvider`` implementation."""

    def __init__(self, answer: str) -> None:
        self._answer = answer

    async def complete(self, *, messages, model, temperature=0.2, **kwargs):
        # The plain (model-only) prompt must NOT carry the RAG system
        # prompt's "Reference documents" framing.
        system = messages[0].content
        assert "参考资料" not in system, "model-only path leaked the RAG prompt"
        return types.SimpleNamespace(
            content=self._answer, prompt_tokens=3, completion_tokens=5
        )

    async def stream(self, *, messages, model, temperature=0.2, **kwargs):
        for piece in self._answer.split():
            yield types.SimpleNamespace(
                delta=piece + " ", prompt_tokens=0, completion_tokens=0
            )


@pytest.fixture
def fake_resolve_chat(monkeypatch):
    """Patch ``_resolve_chat`` so no real provider/DB lookup happens."""

    def _install(answer: str = "巴黎是法国的首都。"):
        async def _fake(session, *, organization_id, model_id, binding_id):
            return types.SimpleNamespace(
                provider=_FakeProvider(answer),
                model=types.SimpleNamespace(model_id="fake-model"),
            )

        monkeypatch.setattr(generate_module, "_resolve_chat", _fake)
        return answer

    return _install


@pytest.mark.asyncio
async def test_empty_scope_answers_from_model_directly(fake_resolve_chat):
    expected = fake_resolve_chat()
    org_id = uuid.uuid4()

    result = await generate_module.generate_answer(
        None,
        organization_id=org_id,
        query="法国的首都是哪里？",
        chunks=[],
        scope_is_empty=True,
    )

    assert result.system_note == "model_only"
    assert result.answer == expected
    assert result.citations == []


@pytest.mark.asyncio
async def test_searched_but_no_match_returns_canned_message(fake_resolve_chat):
    fake_resolve_chat()
    org_id = uuid.uuid4()

    result = await generate_module.generate_answer(
        None,
        organization_id=org_id,
        query="法国的首都是哪里？",
        chunks=[],
        scope_is_empty=False,
    )

    assert result.system_note == "no_scope"
    assert "没有找到" in result.answer
    assert result.citations == []


@pytest.mark.asyncio
async def test_streaming_empty_scope_answers_from_model_directly(fake_resolve_chat):
    expected = fake_resolve_chat()
    org_id = uuid.uuid4()

    collected = ""
    note = ""
    async for event in generate_module.generate_answer_stream(
        None,
        organization_id=org_id,
        query="法国的首都是哪里？",
        chunks=[],
        scope_is_empty=True,
    ):
        if event.kind == "delta":
            collected += event.data["text"]
        if event.kind == "done":
            note = event.data.get("system_note", "")

    assert collected.strip() == expected.strip()
    assert note == "model_only"
