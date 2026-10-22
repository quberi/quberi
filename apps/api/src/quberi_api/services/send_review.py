"""Review before sending: hold a question's prompt for the person to read and edit.

When someone turns on "check before sending", the server still retrieves and
assembles the prompt as usual, but stops before calling the model. It keeps
the assembled pieces here for a short time and shows them to the person, with
suspected sensitive data highlighted. They may edit any piece of text, drop a
reference passage, then approve (the model is called with *their* version) or
cancel (the model is never called).

What the server decides, whatever the browser sends:

* how many pieces there are (question, earlier turns, reference passages) -
  nothing can be added, only edited or removed;
* the fixed system instructions around them;
* who may continue the request: only the person it was prepared for, in that
  conversation, and only once.

The preview and the real call use the same assembly function
(:func:`quberi_api.services.rag.generate.outgoing_messages`), so what is shown
is what is sent.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Optional

from redis.asyncio import Redis

from quberi_api.services.ai.base import ChatMessage
from quberi_api.services.errors import InvalidInputError, NotFoundError
from quberi_api.services.rag.generate import (
    _CHUNK_PREVIEW_MAX,
    _trim_chunk,
    outgoing_messages,
)
from quberi_api.services.rag.vector_store import RetrievedChunk
from quberi_api.services.sensitive import find_sensitive

TTL_SECONDS = 900
# Per-piece and overall caps keep one request bounded.
MAX_QUESTION_CHARS = 8_000
MAX_HISTORY_CHARS = 20_000
MAX_TOTAL_CHARS = 120_000
_KEY = "quberi:send-review:"


# ---------------------------------------------------------------------------
# What is held while the person reviews
# ---------------------------------------------------------------------------


@dataclass
class PendingReview:
    id: str
    organization_id: str
    user_id: str
    conversation_id: str
    user_message_id: str
    correlation_id: str
    query: str
    # The turns that would be sent (already trimmed to the amount sent).
    history: list[dict]
    chunks: list[dict]
    scope_is_empty: bool
    scope_mode: str
    scope_space_ids: list[str]
    # Whether the answer may also carry pictures that might be related (the person's choice
    # when the question was asked; a prompt held before this field existed keeps the default).
    with_pictures: bool = True
    model_id: Optional[str] = None
    binding_id: Optional[str] = None
    model: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)

    @staticmethod
    def from_json(raw: str) -> PendingReview:
        return PendingReview(**json.loads(raw))


def chunk_to_dict(c: RetrievedChunk) -> dict:
    return {
        "document_id": str(c.document_id),
        "space_id": str(c.space_id),
        "document_name": c.document_name,
        "chunk_index": c.chunk_index,
        "text": c.text,
        "score": c.score,
        "char_start": c.char_start,
        "char_end": c.char_end,
        "source_kind": c.source_kind,
        "page_start": c.page_start,
        "page_end": c.page_end,
        "unit_start": c.unit_start,
        "unit_end": c.unit_end,
        "unit_label": c.unit_label,
    }


def chunk_from_dict(d: dict) -> RetrievedChunk:
    # A review held in Redis before positions existed has none of the last six keys; it
    # reads back as plain text with no page, exactly as it was when it was held.
    return RetrievedChunk(
        document_id=uuid.UUID(d["document_id"]),
        space_id=uuid.UUID(d["space_id"]),
        document_name=d["document_name"],
        chunk_index=d["chunk_index"],
        text=d["text"],
        score=d["score"],
        char_start=d["char_start"],
        char_end=d["char_end"],
        source_kind=d.get("source_kind", "text"),
        page_start=d.get("page_start"),
        page_end=d.get("page_end"),
        unit_start=d.get("unit_start"),
        unit_end=d.get("unit_end"),
        unit_label=d.get("unit_label") or "",
    )


def new_pending_id() -> str:
    return uuid.uuid4().hex


async def save(redis: Redis, pending: PendingReview) -> None:
    await redis.set(_KEY + pending.id, pending.to_json(), ex=TTL_SECONDS)


async def _load(
    redis: Redis,
    *,
    pending_id: str,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> PendingReview:
    raw = await redis.get(_KEY + pending_id)
    missing = NotFoundError(
        "这次检查已过期或已处理，请重新提问。 "
        "This review has expired or was already handled; please ask again."
    )
    if raw is None:
        raise missing
    pending = PendingReview.from_json(raw)
    # Same answer for "not yours" and "does not exist".
    if (
        pending.organization_id != str(organization_id)
        or pending.user_id != str(user_id)
        or pending.conversation_id != str(conversation_id)
    ):
        raise missing
    return pending


async def peek(redis: Redis, **kw) -> PendingReview:
    """Read a pending review without using it up (for live previews)."""

    return await _load(redis, **kw)


async def take(redis: Redis, **kw) -> PendingReview:
    """Read a pending review and use it up; a second caller gets nothing.

    ``DEL`` reports whether *this* call removed the key, so two simultaneous
    approvals cannot both go through.
    """

    pending = await _load(redis, **kw)
    if await redis.delete(_KEY + pending.id) != 1:
        raise NotFoundError(
            "这次检查已过期或已处理，请重新提问。 "
            "This review has expired or was already handled; please ask again."
        )
    return pending


# ---------------------------------------------------------------------------
# The person's version
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceEdit:
    index: int  # 1-based, as shown
    include: bool = True
    text: Optional[str] = None


@dataclass(frozen=True)
class ReviewEdits:
    question: Optional[str] = None
    history: Optional[list[Optional[str]]] = None
    references: list[ReferenceEdit] = field(default_factory=list)


@dataclass(frozen=True)
class ResolvedReview:
    """The pieces that will actually go out, plus how they differ from the original."""

    query: str
    history: list[ChatMessage]
    chunks: list[RetrievedChunk]  # included passages, original text, in order
    text_overrides: dict[int, str]  # new 1-based number -> edited prompt text
    excluded_references: int
    edited_parts: int


def shown_reference_text(chunk: dict) -> str:
    """The passage as the prompt would carry it (trimmed like the real thing)."""

    return _trim_chunk(chunk["text"])


def resolve(pending: PendingReview, edits: Optional[ReviewEdits]) -> ResolvedReview:
    """Combine the held prompt with the person's edits, checking every piece."""

    edits = edits or ReviewEdits()

    query = pending.query
    edited = 0
    if edits.question is not None and edits.question != pending.query:
        if not edits.question.strip():
            raise InvalidInputError("问题不能为空。 The question cannot be empty.")
        if len(edits.question) > MAX_QUESTION_CHARS:
            raise InvalidInputError("问题太长。 The question is too long.")
        query = edits.question
        edited += 1

    history: list[ChatMessage] = []
    submitted = edits.history
    if submitted is not None and len(submitted) != len(pending.history):
        raise InvalidInputError("历史对话与原始内容不一致。 History does not match.")
    for i, turn in enumerate(pending.history):
        content = turn["content"]
        new = submitted[i] if submitted is not None else None
        if new is not None and new != content:
            if len(new) > MAX_HISTORY_CHARS:
                raise InvalidInputError("历史对话太长。 A history turn is too long.")
            content = new
            edited += 1
        # An emptied turn is dropped rather than sent as a blank message.
        if content.strip():
            history.append(ChatMessage(role=turn["role"], content=content))

    by_index = {}
    for ref in edits.references:
        if not 1 <= ref.index <= len(pending.chunks) or ref.index in by_index:
            raise InvalidInputError("参考资料编号无效。 Invalid reference number.")
        by_index[ref.index] = ref

    included: list[RetrievedChunk] = []
    overrides: dict[int, str] = {}
    excluded = 0
    for i, raw in enumerate(pending.chunks, start=1):
        ref = by_index.get(i)
        if ref is not None and not ref.include:
            excluded += 1
            continue
        included.append(chunk_from_dict(raw))
        if ref is not None and ref.text is not None and ref.text != shown_reference_text(raw):
            if len(ref.text) > _CHUNK_PREVIEW_MAX:
                raise InvalidInputError(
                    f"参考资料不能超过 {_CHUNK_PREVIEW_MAX} 个字符。 "
                    f"A reference passage can be at most {_CHUNK_PREVIEW_MAX} characters."
                )
            overrides[len(included)] = ref.text
            edited += 1

    total = (
        len(query)
        + sum(len(m.content) for m in history)
        + sum(len(t) for t in overrides.values())
    )
    if total > MAX_TOTAL_CHARS:
        raise InvalidInputError("内容太长。 The content is too long.")
    return ResolvedReview(
        query=query,
        history=history,
        chunks=included,
        text_overrides=overrides,
        excluded_references=excluded,
        edited_parts=edited,
    )


def final_messages(
    pending: PendingReview, resolved: ResolvedReview
) -> Optional[list[ChatMessage]]:
    """What would be sent for this version of the prompt (``None``: no model call)."""

    return outgoing_messages(
        query=resolved.query,
        chunks=resolved.chunks,
        history=resolved.history,
        scope_is_empty=pending.scope_is_empty,
        text_overrides=resolved.text_overrides,
    )


# ---------------------------------------------------------------------------
# What the screen gets
# ---------------------------------------------------------------------------


def _spans(text: str) -> list[dict]:
    return [{"start": s.start, "end": s.end, "kind": s.kind} for s in find_sensitive(text)]


def build_payload(
    pending: PendingReview, edits: Optional[ReviewEdits] = None, *, with_meta: bool = True
) -> dict:
    """The review card's data for the original prompt, or for the given edits.

    Texts are the *submitted* ones, and the highlighted spans are offsets into
    exactly those texts, so the screen can mark them as the person types.
    """

    resolved = resolve(pending, edits)
    edits = edits or ReviewEdits()
    by_index = {r.index: r for r in edits.references}

    # Every piece is listed, including dropped passages, so the card keeps them.
    references = []
    kept = 0
    for i, raw in enumerate(pending.chunks, start=1):
        ref = by_index.get(i)
        included = ref is None or ref.include
        text = shown_reference_text(raw)
        if included:
            kept += 1
            text = resolved.text_overrides.get(kept, text)
        references.append(
            {
                "index": i,
                "document_name": raw["document_name"],
                "chunk_index": raw["chunk_index"],
                "included": included,
                "text": text,
                "spans": _spans(text) if included else [],
            }
        )
    history_texts = []
    submitted = edits.history
    for i, turn in enumerate(pending.history):
        new = submitted[i] if submitted is not None else None
        content = new if new is not None else turn["content"]
        history_texts.append(
            {"role": turn["role"], "text": content, "spans": _spans(content)}
        )

    messages = final_messages(pending, resolved)
    payload = {
        "question": {"text": resolved.query, "spans": _spans(resolved.query)},
        "history": history_texts,
        "references": references,
        "messages": [{"role": m.role, "content": m.content} for m in messages]
        if messages is not None
        else None,
        "edited_parts": resolved.edited_parts,
        "excluded_references": resolved.excluded_references,
    }
    payload["sensitive_count"] = (
        len(payload["question"]["spans"])
        + sum(len(h["spans"]) for h in history_texts)
        + sum(len(r["spans"]) for r in references)
    )
    if with_meta:
        payload.update(
            pending_id=pending.id,
            expires_in=TTL_SECONDS,
            model=pending.model,
        )
    return payload
