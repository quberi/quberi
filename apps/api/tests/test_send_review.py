"""Review before sending: hold the prompt, let the person edit it, then send.

The properties that matter:

* while a prompt is held, the model is not called and no answer is saved;
* what is previewed is exactly what is sent (same assembly function);
* the person's edits reach the model, while citations keep the original text;
* a dropped passage never reaches the model and the rest are renumbered;
* cancelling never calls the model;
* a held prompt can be used once, only by its owner, only in its conversation;
* a bad edit is refused without losing the held prompt.

The model and retrieval are faked, so nothing leaves the machine.
"""

from __future__ import annotations

import types
import uuid

import pytest
from sqlalchemy import select

from quberi_api.api.v1.conversations import router as conv_router
from quberi_api.api.v1.conversations.schemas import SendMessageRequest
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.conversations.models import Message
from quberi_api.services import conversations as conv
from quberi_api.services import members as members_service
from quberi_api.services import send_review
from quberi_api.services.ai.base import ChatStreamChunk
from quberi_api.services.errors import InvalidInputError, NotFoundError
from quberi_api.services.rag import generate as generate_module
from quberi_api.services.rag.retrieve import ScopedRetrieval
from quberi_api.services.rag.vector_store import RetrievedChunk

PHONE = "13812345678"
SECRET_PASSAGE = "内部预算表：差旅预算 80 万，负责人电话 13900001111。"
PASSAGE_A = f"报销流程：联系财务王芳，电话 {PHONE}，邮件 wang@example.com。"
PASSAGE_B = "第二段：与手机号无关的普通制度说明。"


class FakeProvider:
    """Records exactly what the model would have been sent."""

    def __init__(self) -> None:
        self.calls: list[list] = []

    async def stream(self, *, messages, model, temperature=0.2, max_tokens=None):
        self.calls.append([(m.role, m.content) for m in messages])
        yield ChatStreamChunk(delta="好的 [1]", prompt_tokens=3, completion_tokens=2)

    async def complete(self, *, messages, model, temperature=0.2, **kw):  # pragma: no cover
        self.calls.append([(m.role, m.content) for m in messages])
        return types.SimpleNamespace(content="ok", prompt_tokens=1, completion_tokens=1)


@pytest.fixture
def model(monkeypatch):
    provider = FakeProvider()
    spec = types.SimpleNamespace(
        model_id="fake-model", display_name="Fake Model", vendor="fakevendor"
    )

    async def fake_resolve(*_a, **_k):
        return types.SimpleNamespace(provider=provider, model=spec)

    monkeypatch.setattr(generate_module, "_resolve_chat", fake_resolve)
    return provider


class _Q:
    async def close(self):
        return None


def _chunk(doc, space, text, i=0):
    return RetrievedChunk(
        document_id=doc, space_id=space, document_name=f"doc-{i}.docx", chunk_index=i,
        text=text, score=0.9, char_start=0, char_end=len(text),
    )


@pytest.fixture
def retrieval(monkeypatch, org_and_owner):
    """Two retrieved passages (the first holds a phone number and an e-mail)."""

    docs = [uuid.uuid4(), uuid.uuid4()]
    space = org_and_owner.personal_space_id
    state = types.SimpleNamespace(chunks=[_chunk(docs[0], space, PASSAGE_A, 0),
                                          _chunk(docs[1], space, PASSAGE_B, 1)],
                                  spaces=[space], docs=docs)

    async def fake_retrieve(*_a, **_k):
        return ScopedRetrieval(chunks=state.chunks, space_ids=state.spaces,
                               scope_mode="auto", model_id="m", collection="c")

    monkeypatch.setattr(conv, "retrieve_for_conversation", fake_retrieve)
    monkeypatch.setattr(conv, "create_qdrant_client", lambda _s: _Q())
    return state


async def _conversation(db, o):
    await db.commit()
    view = await conv.create_conversation(
        db, space_id=o.personal_space_id, organization_id=o.organization_id,
        user_id=o.user_id, title="t",
    )
    # Keep only the id: the ORM object would be expired by later rollbacks.
    cid = view.conversation.id
    await db.commit()
    return types.SimpleNamespace(id=cid)


async def _events(gen):
    return [(k, d) async for k, d in gen]


async def _send(db, o, c, text, *, review=True):
    return await _events(conv.send_message_stream(
        db, conversation_id=c.id, organization_id=o.organization_id, user_id=o.user_id,
        identity_id=None, text=text, review=review,
    ))


def _ids(o, c, pending_id):
    return dict(conversation_id=c.id, organization_id=o.organization_id,
                user_id=o.user_id, pending_id=pending_id)


def _unchanged(payload) -> send_review.ReviewEdits:
    return send_review.ReviewEdits(
        question=payload["question"]["text"],
        history=[h["text"] for h in payload["history"]],
        references=[send_review.ReferenceEdit(index=r["index"], include=r["included"],
                                              text=r["text"]) for r in payload["references"]],
    )


async def _approve(db, o, c, pending_id, edits, identity_id=None):
    await db.commit()
    return await _events(conv.resume_reviewed_stream(
        db, identity_id=identity_id, edits=edits, **_ids(o, c, pending_id)))


async def _messages(db, c):
    await db.commit()
    return (await db.execute(
        select(Message).where(Message.conversation_id == c.id).order_by(Message.created_at)
    )).scalars().all()


async def _audit(db, action, conversation_id):
    await db.commit()
    return (await db.execute(
        select(AuditEvent).where(AuditEvent.action == action,
                                 AuditEvent.resource_id == conversation_id)
    )).scalars().all()


# ---------------------------------------------------------------------------
# Holding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_holds_the_prompt_without_calling_the_model(
    db_session, org_and_owner, model, retrieval
):
    c = await _conversation(db_session, org_and_owner)
    events = await _send(db_session, org_and_owner, c, f"请联系 {PHONE} 的同事")
    kinds = [k for k, _ in events]
    assert kinds == ["user_message", "review_required", "done"]
    assert model.calls == []  # nothing left for the model
    stored = await _messages(db_session, c)
    assert [m.role for m in stored] == ["user"]  # no assistant answer yet

    payload = dict(events)["review_required"]
    assert payload["model"] == {"model_id": "fake-model", "display_name": "Fake Model",
                                "vendor": "fakevendor"}
    assert payload["question"]["text"] == f"请联系 {PHONE} 的同事"
    kinds_found = {s["kind"] for s in payload["question"]["spans"]}
    assert kinds_found == {"phone"}
    ref0 = payload["references"][0]
    assert {s["kind"] for s in ref0["spans"]} == {"phone", "email"}
    for s in ref0["spans"]:  # offsets index into exactly the text shown
        assert ref0["text"][s["start"]:s["end"]] in (PHONE, "wang@example.com")
    assert payload["sensitive_count"] == 3


@pytest.mark.asyncio
async def test_no_match_needs_no_review_and_answers_directly(
    db_session, org_and_owner, model, retrieval
):
    retrieval.chunks = []  # searched, nothing matched: no model call at all
    c = await _conversation(db_session, org_and_owner)
    events = await _send(db_session, org_and_owner, c, "没有资料的问题")
    kinds = [k for k, _ in events]
    assert "review_required" not in kinds and "assistant_message" in kinds
    assert model.calls == []


@pytest.mark.asyncio
async def test_empty_scope_review_shows_the_plain_prompt(
    db_session, org_and_owner, model, retrieval
):
    retrieval.chunks, retrieval.spaces = [], []  # no space selected: model-only
    c = await _conversation(db_session, org_and_owner)
    events = await _send(db_session, org_and_owner, c, "直接问模型")
    payload = dict(events)["review_required"]
    assert payload["references"] == []
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    assert model.calls == []


# ---------------------------------------------------------------------------
# Approving
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unedited_approval_sends_exactly_what_was_previewed(
    db_session, org_and_owner, model, retrieval
):
    c = await _conversation(db_session, org_and_owner)
    payload = dict(await _send(db_session, org_and_owner, c, "报销流程是什么？"))["review_required"]
    out = await _approve(db_session, org_and_owner, c, payload["pending_id"], _unchanged(payload))

    assert [k for k, _ in out][-2:] == ["assistant_message", "done"]
    assert len(model.calls) == 1
    previewed = [(m["role"], m["content"]) for m in payload["messages"]]
    assert model.calls[0] == previewed  # what was shown is what went out
    assert PHONE in model.calls[0][0][1]  # unedited: still in the prompt

    audit = (await _audit(db_session, "message.sent", c.id))[0].payload
    assert audit["llm_review"] == {"outcome": "approved", "edited_parts": 0,
                                   "excluded_references": 0, "excluded_document_ids": []}
    assert audit["retrieved_document_ids"] == [str(d) for d in retrieval.docs]
    saved = [m for m in await _messages(db_session, c) if m.role == "assistant"]
    assert len(saved) == 1 and saved[0].content["meta"]["review"]["edited_parts"] == 0


@pytest.mark.asyncio
async def test_edits_reach_the_model_but_citations_keep_the_original(
    db_session, org_and_owner, model, retrieval
):
    c = await _conversation(db_session, org_and_owner)
    payload = dict(
        await _send(db_session, org_and_owner, c, f"{PHONE} 是谁的电话")
    )["review_required"]
    edits = send_review.ReviewEdits(
        question="王芳的电话是谁的",
        history=[],
        references=[
            send_review.ReferenceEdit(index=1, include=True,
                                      text="报销流程：联系财务王芳，电话 [已隐藏]。"),
            send_review.ReferenceEdit(index=2, include=True, text=payload["references"][1]["text"]),
        ],
    )
    out = await _approve(db_session, org_and_owner, c, payload["pending_id"], edits)

    sent = "\n".join(content for _, content in model.calls[0])
    assert PHONE not in sent and "wang@example.com" not in sent
    assert "[已隐藏]" in sent and "王芳的电话是谁的" in sent
    # The citation list still shows the original passage, flagged as edited via meta.
    retrieved = dict(out)["retrieved"]
    assert retrieved["citations"][0]["text"] == PASSAGE_A
    message = dict(out)["assistant_message"]
    assert message["content"]["meta"]["review"]["edited_parts"] == 2
    # ... and so does the reply as saved (not just the live stream).
    assert message["content"]["citations"][0]["text"] == PASSAGE_A
    saved = [m for m in await _messages(db_session, c) if m.role == "assistant"][0]
    assert saved.content["citations"][0]["text"] == PASSAGE_A
    # The message that was saved for the user keeps what they typed.
    stored_user = [m for m in await _messages(db_session, c) if m.role == "user"][0]
    assert stored_user.content["text"] == f"{PHONE} 是谁的电话"


@pytest.mark.asyncio
async def test_a_dropped_passage_never_reaches_the_model_and_the_rest_renumber(
    db_session, org_and_owner, model, retrieval
):
    retrieval.chunks[0] = _chunk(retrieval.docs[0], org_and_owner.personal_space_id,
                                 SECRET_PASSAGE, 0)
    c = await _conversation(db_session, org_and_owner)
    payload = dict(await _send(db_session, org_and_owner, c, "制度是什么"))["review_required"]
    edits = send_review.ReviewEdits(
        question="制度是什么", history=[],
        references=[send_review.ReferenceEdit(index=1, include=False),
                    send_review.ReferenceEdit(index=2, include=True)],
    )
    out = await _approve(db_session, org_and_owner, c, payload["pending_id"], edits)

    sent = "\n".join(content for _, content in model.calls[0])
    assert "差旅预算" not in sent and "13900001111" not in sent
    assert PASSAGE_B in sent and "[1] 文件：doc-1.docx" in sent  # renumbered to [1]
    citations = dict(out)["retrieved"]["citations"]
    assert [(x["index"], x["document_name"]) for x in citations] == [(1, "doc-1.docx")]
    audit = (await _audit(db_session, "message.sent", c.id))[0].payload["llm_review"]
    assert audit["excluded_references"] == 1
    assert audit["excluded_document_ids"] == [str(retrieval.docs[0])]


@pytest.mark.asyncio
async def test_preview_with_edits_matches_what_approval_sends(
    db_session, org_and_owner, model, retrieval
):
    c = await _conversation(db_session, org_and_owner)
    payload = dict(await _send(db_session, org_and_owner, c, "报销流程"))["review_required"]
    edits = send_review.ReviewEdits(
        question="报销流程（已改写）", history=[],
        references=[send_review.ReferenceEdit(index=1, include=True, text="只保留这一句。"),
                    send_review.ReferenceEdit(index=2, include=False)],
    )
    await db_session.commit()
    preview = await conv.preview_review(db_session, edits=edits,
                                        **_ids(org_and_owner, c, payload["pending_id"]))
    assert model.calls == []  # a preview sends nothing and uses nothing up
    await _approve(db_session, org_and_owner, c, payload["pending_id"], edits)
    assert model.calls[0] == [(m["role"], m["content"]) for m in preview["messages"]]
    assert preview["edited_parts"] == 2 and preview["excluded_references"] == 1


@pytest.mark.asyncio
async def test_the_preview_highlights_follow_the_edited_text(
    db_session, org_and_owner, model, retrieval
):
    c = await _conversation(db_session, org_and_owner)
    payload = dict(await _send(db_session, org_and_owner, c, "q"))["review_required"]
    edits = send_review.ReviewEdits(
        question="我的邮箱是 a@b.co，手机 13700001111", history=[],
        references=[send_review.ReferenceEdit(index=1, include=True, text="干净的文本"),
                    send_review.ReferenceEdit(index=2, include=True,
                                              text=payload["references"][1]["text"])],
    )
    await db_session.commit()
    preview = await conv.preview_review(db_session, edits=edits,
                                        **_ids(org_and_owner, c, payload["pending_id"]))
    q = preview["question"]
    assert [q["text"][s["start"]:s["end"]] for s in q["spans"]] == ["a@b.co", "13700001111"]
    assert preview["references"][0]["spans"] == []  # the number was edited out


# ---------------------------------------------------------------------------
# Cancelling, single use, ownership
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_never_calls_the_model_and_leaves_a_note(
    db_session, org_and_owner, model, retrieval
):
    c = await _conversation(db_session, org_and_owner)
    payload = dict(await _send(db_session, org_and_owner, c, "q"))["review_required"]
    await db_session.commit()
    note = await conv.cancel_review(db_session, **_ids(org_and_owner, c, payload["pending_id"]))
    assert model.calls == []
    assert note["content"]["meta"]["review"]["cancelled"] is True
    assert len(await _audit(db_session, "message.send_cancelled", c.id)) == 1
    assert await _audit(db_session, "message.sent", c.id) == []
    with pytest.raises(NotFoundError):  # gone: cannot be approved afterwards
        await _approve(db_session, org_and_owner, c, payload["pending_id"], _unchanged(payload))
    assert model.calls == []


@pytest.mark.asyncio
async def test_a_held_prompt_can_be_sent_only_once(
    db_session, org_and_owner, model, retrieval
):
    c = await _conversation(db_session, org_and_owner)
    payload = dict(await _send(db_session, org_and_owner, c, "q"))["review_required"]
    await _approve(db_session, org_and_owner, c, payload["pending_id"], _unchanged(payload))
    with pytest.raises(NotFoundError):
        await _approve(db_session, org_and_owner, c, payload["pending_id"], _unchanged(payload))
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_only_the_owner_in_the_same_conversation_may_continue(
    db_session, org_and_owner, model, retrieval
):
    c = await _conversation(db_session, org_and_owner)
    other_conv = await _conversation(db_session, org_and_owner)
    payload = dict(await _send(db_session, org_and_owner, c, "q"))["review_required"]
    pid = payload["pending_id"]

    # Same user, different conversation.
    with pytest.raises(NotFoundError):
        await _approve(db_session, org_and_owner, other_conv, pid, _unchanged(payload))
    # Another member of the organisation.
    await db_session.commit()
    stranger_id = (await members_service.invite_member(
        db_session, organization_id=org_and_owner.organization_id,
        invited_by_user_id=org_and_owner.user_id, email=f"s-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Stranger", role_code="member")).user.id
    await db_session.commit()
    with pytest.raises(Exception) as exc:
        await _events(conv.resume_reviewed_stream(
            db_session, conversation_id=c.id, organization_id=org_and_owner.organization_id,
            user_id=stranger_id, identity_id=None, pending_id=pid, edits=_unchanged(payload)))
    assert isinstance(exc.value, NotFoundError)
    with pytest.raises(NotFoundError):
        await conv.cancel_review(db_session, conversation_id=c.id,
                                 organization_id=org_and_owner.organization_id,
                                 user_id=stranger_id, pending_id=pid)
    assert model.calls == []
    # The rightful owner can still use it: the failed attempts did not consume it.
    await _approve(db_session, org_and_owner, c, pid, _unchanged(payload))
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_the_held_prompt_itself_is_bound_to_its_owner_org_and_conversation(
    db_session, org_and_owner, model, retrieval
):
    """The guard in the store, whatever the layers above it check."""

    from quberi_api.core.config import get_settings
    from quberi_api.infrastructure.redis import create_redis_client

    c = await _conversation(db_session, org_and_owner)
    payload = dict(await _send(db_session, org_and_owner, c, "q"))["review_required"]
    ok = dict(
        pending_id=payload["pending_id"],
        organization_id=org_and_owner.organization_id,
        user_id=org_and_owner.user_id,
        conversation_id=c.id,
    )
    redis = create_redis_client(get_settings())
    try:
        for field in ("organization_id", "user_id", "conversation_id"):
            wrong = {**ok, field: uuid.uuid4()}
            with pytest.raises(NotFoundError):
                await send_review.peek(redis, **wrong)
            with pytest.raises(NotFoundError):
                await send_review.take(redis, **wrong)
        # The refused attempts left it intact for its rightful owner.
        assert (await send_review.peek(redis, **ok)).id == payload["pending_id"]
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_an_expired_or_unknown_review_is_not_found(
    db_session, org_and_owner, model, retrieval
):
    c = await _conversation(db_session, org_and_owner)
    payload = dict(await _send(db_session, org_and_owner, c, "q"))["review_required"]
    with pytest.raises(NotFoundError):
        await _approve(db_session, org_and_owner, c, "0" * 32, _unchanged(payload))


# ---------------------------------------------------------------------------
# Bad edits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_edits_are_refused_and_do_not_use_up_the_held_prompt(
    db_session, org_and_owner, model, retrieval
):
    c = await _conversation(db_session, org_and_owner)
    payload = dict(await _send(db_session, org_and_owner, c, "q"))["review_required"]
    pid = payload["pending_id"]
    good = _unchanged(payload)

    bad_versions = [
        send_review.ReviewEdits(question="   ", references=good.references, history=good.history),
        send_review.ReviewEdits(question="q", history=["extra"], references=good.references),
        send_review.ReviewEdits(question="q", history=[],
                                references=[send_review.ReferenceEdit(index=9)]),
        send_review.ReviewEdits(question="q", history=[],
                                references=[send_review.ReferenceEdit(index=1),
                                            send_review.ReferenceEdit(index=1)]),
        send_review.ReviewEdits(question="q", history=[],
                                references=[send_review.ReferenceEdit(index=1, text="长" * 1201)]),
    ]
    for bad in bad_versions:
        with pytest.raises(InvalidInputError):
            await _approve(db_session, org_and_owner, c, pid, bad)
    assert model.calls == []
    await _approve(db_session, org_and_owner, c, pid, good)  # still usable
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_the_approve_endpoint_refuses_bad_edits_before_streaming(
    db_session, org_and_owner, model, retrieval
):
    """A bad edit must be an HTTP error, not an error hidden inside a 200 stream."""

    from quberi_api.api.security import CallerContext
    from quberi_api.api.v1.conversations.schemas import (
        ReviewEditsRequest,
        ReviewReferenceEdit,
    )
    from quberi_api.domain.identity.models import Organization, User

    c = await _conversation(db_session, org_and_owner)
    payload = dict(await _send(db_session, org_and_owner, c, "q"))["review_required"]
    organization = await db_session.get(Organization, org_and_owner.organization_id)
    user = await db_session.get(User, org_and_owner.user_id)
    caller = CallerContext(user=user, organization=organization, is_platform_admin=False)

    bad = ReviewEditsRequest(question="   ", history=[], references=[])
    with pytest.raises(InvalidInputError):
        await conv_router.approve_review(c.id, payload["pending_id"], bad,
                                         caller=caller, session=db_session)
    await db_session.commit()
    with pytest.raises(NotFoundError):
        await conv_router.approve_review(c.id, "0" * 32, bad.model_copy(update={"question": "q"}),
                                         caller=caller, session=db_session)
    assert model.calls == []
    # A good request gets a streaming response and the prompt is still there.
    good = ReviewEditsRequest(
        question="q", history=[],
        references=[ReviewReferenceEdit(index=1, include=True, text=None)],
    )
    await db_session.commit()
    response = await conv_router.approve_review(c.id, payload["pending_id"], good,
                                                caller=caller, session=db_session)
    assert response.media_type == "text/event-stream"


@pytest.mark.asyncio
async def test_dropping_every_passage_calls_no_model(
    db_session, org_and_owner, model, retrieval
):
    c = await _conversation(db_session, org_and_owner)
    payload = dict(await _send(db_session, org_and_owner, c, "q"))["review_required"]
    edits = send_review.ReviewEdits(
        question="q", history=[],
        references=[send_review.ReferenceEdit(index=1, include=False),
                    send_review.ReferenceEdit(index=2, include=False)],
    )
    out = await _approve(db_session, org_and_owner, c, payload["pending_id"], edits)
    assert model.calls == []  # the fixed "nothing relevant found" reply, no model
    assert "assistant_message" in dict(out)


# ---------------------------------------------------------------------------
# Not supported / unchanged paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_conversations_refuse_review_instead_of_skipping_it(
    db_session, org_and_owner, model, retrieval
):
    from quberi_api.services.agents import config as agents_config

    await db_session.commit()
    await agents_config.ensure_seeded(db_session, organization_id=org_and_owner.organization_id)
    await db_session.commit()
    agent = await agents_config.get_default_agent(
        db_session, organization_id=org_and_owner.organization_id)
    await db_session.commit()
    c = await _conversation(db_session, org_and_owner)
    await conv.set_conversation_agent(
        db_session, conversation_id=c.id, organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id, agent_id=agent.id)
    await db_session.commit()
    with pytest.raises(InvalidInputError):
        await _send(db_session, org_and_owner, c, "q", review=True)
    assert [m for m in await _messages(db_session, c) if m.role == "user"] == []  # rolled back
    assert model.calls == []


@pytest.mark.asyncio
async def test_normal_streaming_without_review_is_unchanged(
    db_session, org_and_owner, model, retrieval
):
    c = await _conversation(db_session, org_and_owner)
    events = await _send(db_session, org_and_owner, c, "报销流程", review=False)
    kinds = [k for k, _ in events]
    assert kinds[0] == "user_message" and kinds[-2:] == ["assistant_message", "done"]
    assert "retrieved" in kinds and "delta" in kinds and "review_required" not in kinds
    assert len(model.calls) == 1
    audit = (await _audit(db_session, "message.sent", c.id))[0].payload
    assert audit["streamed"] is True
    assert audit["retrieved_document_ids"] == [str(d) for d in retrieval.docs]
    assert "llm_review" not in audit


@pytest.mark.asyncio
async def test_the_non_streaming_endpoint_refuses_review_rather_than_ignoring_it(
    db_session, org_and_owner
):
    from fastapi import HTTPException

    from quberi_api.api.security import CallerContext
    from quberi_api.domain.identity.models import Organization, User

    organization = await db_session.get(Organization, org_and_owner.organization_id)
    user = await db_session.get(User, org_and_owner.user_id)
    caller = CallerContext(user=user, organization=organization, is_platform_admin=False)
    with pytest.raises(HTTPException) as exc:
        await conv_router.send_message(
            uuid.uuid4(), SendMessageRequest(text="hi", review=True),
            caller=caller, session=db_session)
    assert exc.value.status_code == 422
