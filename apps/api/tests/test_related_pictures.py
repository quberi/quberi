"""Every ordinary question is also looked up in the picture index.

"动物母狮" has no picture word in it, so it cannot be told apart from a question about a
document; a text model cannot read pictures and would answer "nothing found". So the answer
carries the closest pictures beside it, as ``image_matches``, when one scores well enough.

The properties that matter: only what the person may see appears; nothing is looked up when
there is nothing to find or the question is a paragraph; any failure leaves the answer as it
was; and a held ("check before sending") prompt still sends nothing extra to the model.
Retrieval and the model are faked; the picture store is qdrant in memory.
"""

from __future__ import annotations

import types
import uuid

import pytest
from sqlalchemy import select

from quberi_api.core.config import get_settings
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.conversations.models import Message
from quberi_api.services import conversations as conv
from quberi_api.services.rag import image_search
from quberi_api.services.rag.retrieve import ScopedRetrieval
from tests.test_image_indexing import _space
from tests.test_image_search import BLUE, RED
from tests.test_image_search_api import (  # noqa: F401  (fixtures)
    _conversation,
    _picture,
    client,
    fake_embedder,
    picture_store,
    qdrant,
    redis_client,
)
from tests.test_send_review import FakeProvider, model  # noqa: F401  (fixture)


def _args(o, conv_id, text):
    return dict(
        conversation_id=conv_id, organization_id=o.organization_id, user_id=o.user_id, text=text
    )


async def test_a_short_question_gets_the_closest_picture(db_session, org_and_owner, qdrant):
    space = await _space(db_session, org_and_owner, "Animals")
    await _picture(db_session, org_and_owner, qdrant, space, BLUE, "lioness.png")
    await _picture(db_session, org_and_owner, qdrant, space, RED, "other.png")
    cid = (await _conversation(db_session, org_and_owner)).id

    found = await conv.related_pictures(db_session, **_args(org_and_owner, cid, "动物母狮"))

    # The stand-in embeds an unknown word as blue.
    assert [m["document_name"] for m in found] == ["lioness.png"]
    assert found[0]["score"] >= conv.RELATED_PICTURE_MIN_SCORE
    assert found[0]["space_name"] == "Animals" and found[0]["mime_type"] == "image/png"


async def test_a_picture_that_does_not_match_is_left_out(db_session, org_and_owner, qdrant):
    space = await _space(db_session, org_and_owner, "Animals")
    await _picture(db_session, org_and_owner, qdrant, space, RED, "red.png")
    cid = (await _conversation(db_session, org_and_owner)).id

    assert await conv.related_pictures(db_session, **_args(org_and_owner, cid, "动物母狮")) == []


async def test_at_most_a_few_are_offered(db_session, org_and_owner, qdrant):
    space = await _space(db_session, org_and_owner, "Animals")
    for i in range(conv.RELATED_PICTURE_TOP_K + 3):
        await _picture(db_session, org_and_owner, qdrant, space, BLUE, f"{i}.png")
    cid = (await _conversation(db_session, org_and_owner)).id

    found = await conv.related_pictures(db_session, **_args(org_and_owner, cid, "动物母狮"))

    assert len(found) == conv.RELATED_PICTURE_TOP_K


async def test_nothing_is_looked_up_when_there_is_nothing_to_find(
    db_session, org_and_owner, qdrant, monkeypatch
):
    calls: list[str] = []
    monkeypatch.setattr(
        image_search, "embed_query", lambda **_k: calls.append("embed") or (_ for _ in ()).throw(AssertionError)
    )
    await _space(db_session, org_and_owner, "Docs only")
    cid = (await _conversation(db_session, org_and_owner)).id

    assert await conv.related_pictures(db_session, **_args(org_and_owner, cid, "动物母狮")) == []
    assert calls == []  # the picture model was not even asked


async def test_a_paragraph_is_not_looked_up(db_session, org_and_owner, qdrant, monkeypatch):
    space = await _space(db_session, org_and_owner, "Animals")
    await _picture(db_session, org_and_owner, qdrant, space, BLUE, "a.png")
    cid = (await _conversation(db_session, org_and_owner)).id
    long_question = "请问" + "报销流程怎么走" * 8
    assert len(long_question) > conv.RELATED_PICTURE_MAX_CHARS

    assert await conv.related_pictures(db_session, **_args(org_and_owner, cid, long_question)) == []
    assert await conv.related_pictures(db_session, **_args(org_and_owner, cid, "   ")) == []


async def test_only_pictures_the_person_may_see_are_offered(db_session, org_and_owner, qdrant):
    from tests.test_image_search import _member

    hidden = await _space(db_session, org_and_owner, "Hidden")
    await _picture(db_session, org_and_owner, qdrant, hidden, BLUE, "hidden.png")
    member = await _member(db_session, org_and_owner)
    personal = (
        await db_session.execute(
            select(conv.Space.id).where(
                conv.Space.owner_id == member.id, conv.Space.preset == "personal"
            )
        )
    ).scalar_one()
    await db_session.commit()
    cid = (
        await conv.create_conversation(
            db_session, space_id=personal, organization_id=org_and_owner.organization_id,
            user_id=member.id, title=None,
        )
    ).conversation.id
    await db_session.commit()

    found = await conv.related_pictures(
        db_session, conversation_id=cid, organization_id=org_and_owner.organization_id,
        user_id=member.id, text="动物母狮",
    )

    assert found == []


async def test_a_conversation_bound_to_an_agent_is_left_to_its_agent(
    db_session, org_and_owner, qdrant, monkeypatch
):
    space = await _space(db_session, org_and_owner, "Animals")
    await _picture(db_session, org_and_owner, qdrant, space, BLUE, "a.png")
    cid = (await _conversation(db_session, org_and_owner)).id
    monkeypatch.setattr(conv, "_agent_binding", lambda _c: uuid.uuid4())

    assert await conv.related_pictures(db_session, **_args(org_and_owner, cid, "动物母狮")) == []


async def test_switched_off_picture_search_offers_nothing(
    db_session, org_and_owner, qdrant, monkeypatch
):
    space = await _space(db_session, org_and_owner, "Animals")
    await _picture(db_session, org_and_owner, qdrant, space, BLUE, "a.png")
    cid = (await _conversation(db_session, org_and_owner)).id
    monkeypatch.setattr(get_settings(), "image_search_enabled", False)

    assert await conv.related_pictures(db_session, **_args(org_and_owner, cid, "动物母狮")) == []


async def test_a_failure_leaves_the_answer_alone_and_the_session_usable(
    db_session, org_and_owner, qdrant, monkeypatch
):
    space = await _space(db_session, org_and_owner, "Animals")
    await _picture(db_session, org_and_owner, qdrant, space, BLUE, "a.png")
    cid = (await _conversation(db_session, org_and_owner)).id

    async def boom(**_k):
        raise RuntimeError("model could not load")

    monkeypatch.setattr(image_search, "embed_query", boom)

    assert await conv.related_pictures(db_session, **_args(org_and_owner, cid, "动物母狮")) == []
    # The session is still good for the next statement.
    assert (await db_session.execute(select(Message.id).limit(1))).first() is None


async def test_a_failed_lookup_does_not_stop_the_streamed_answer_from_being_saved(
    db_session, org_and_owner, qdrant, text_answer, model, monkeypatch
):
    space = await _space(db_session, org_and_owner, "Animals")
    await _picture(db_session, org_and_owner, qdrant, space, BLUE, "lioness.png")
    cid = (await _conversation(db_session, org_and_owner)).id

    async def boom(**_k):
        raise RuntimeError("picture model could not load")

    monkeypatch.setattr(image_search, "embed_query", boom)

    events = await _stream(db_session, org_and_owner, cid, "动物母狮")

    kinds = [k for k, _ in events]
    assert kinds[-2:] == ["assistant_message", "done"] and "error" not in kinds
    answer = next(d for k, d in events if k == "assistant_message")["content"]
    assert "image_matches" not in answer
    count = (
        await db_session.execute(
            select(Message.id).where(Message.conversation_id == cid, Message.role == "assistant")
        )
    ).all()
    assert len(count) == 1


# ---------------------------------------------------------------------------
# In the saved answer
# ---------------------------------------------------------------------------


@pytest.fixture
def text_answer(monkeypatch, org_and_owner):
    """Retrieval that finds one unrelated passage; the model is the shared fake."""
    space = org_and_owner.personal_space_id

    async def fake_retrieve(*_a, **_k):
        return ScopedRetrieval(
            chunks=[], space_ids=[space], scope_mode="auto", model_id="m", collection="c"
        )

    monkeypatch.setattr(conv, "retrieve_for_conversation", fake_retrieve)


async def _stream(db, o, cid, text, **kw):
    return [
        (k, d)
        async for k, d in conv.send_message_stream(
            db, conversation_id=cid, organization_id=o.organization_id, user_id=o.user_id,
            identity_id=o.identity_id, text=text, **kw,
        )
    ]


async def test_the_streamed_answer_carries_the_pictures_and_audits_their_ids(
    db_session, org_and_owner, qdrant, text_answer, model
):
    space = await _space(db_session, org_and_owner, "Animals")
    pic = await _picture(db_session, org_and_owner, qdrant, space, BLUE, "lioness.png")
    cid = (await _conversation(db_session, org_and_owner)).id

    events = await _stream(db_session, org_and_owner, cid, "动物母狮")

    answer = next(d for k, d in events if k == "assistant_message")["content"]
    assert [m["document_id"] for m in answer["image_matches"]] == [str(pic.id)]
    saved = (
        await db_session.execute(
            select(Message.content).where(Message.conversation_id == cid, Message.role == "assistant")
        )
    ).scalar_one()
    assert saved["image_matches"][0]["document_name"] == "lioness.png"
    audit = (
        await db_session.execute(
            select(AuditEvent.payload).where(AuditEvent.action == "message.sent")
        )
    ).scalars().all()[-1]
    assert audit["related_picture_ids"] == [str(pic.id)]
    assert "lioness.png" not in repr(audit)
    # The model was asked the question as usual and was told nothing about pictures.
    assert "lioness" not in repr(model.calls)


async def test_an_answer_with_no_close_picture_has_no_image_matches(
    db_session, org_and_owner, qdrant, text_answer, model
):
    space = await _space(db_session, org_and_owner, "Animals")
    await _picture(db_session, org_and_owner, qdrant, space, RED, "red.png")
    cid = (await _conversation(db_session, org_and_owner)).id

    events = await _stream(db_session, org_and_owner, cid, "动物母狮")

    answer = next(d for k, d in events if k == "assistant_message")["content"]
    assert "image_matches" not in answer


async def test_a_held_prompt_sends_nothing_extra_and_the_pictures_join_the_approved_answer(
    db_session, org_and_owner, qdrant, model, monkeypatch
):
    from quberi_api.services import send_review
    from tests.test_send_review import PASSAGE_A, _approve, _chunk, _unchanged

    space = await _space(db_session, org_and_owner, "Animals")
    await _picture(db_session, org_and_owner, qdrant, space, BLUE, "lioness.png")
    conversation = await _conversation(db_session, org_and_owner)
    doc = uuid.uuid4()

    async def fake_retrieve(*_a, **_k):
        return ScopedRetrieval(
            chunks=[_chunk(doc, org_and_owner.personal_space_id, PASSAGE_A, 0)],
            space_ids=[org_and_owner.personal_space_id], scope_mode="auto", model_id="m",
            collection="c",
        )

    monkeypatch.setattr(conv, "retrieve_for_conversation", fake_retrieve)

    held = await _stream(db_session, org_and_owner, conversation.id, "动物母狮", review=True)

    assert [k for k, _ in held] == ["user_message", "review_required", "done"]
    assert model.calls == []
    payload = dict(held)["review_required"]
    assert "lioness" not in repr(payload)  # the card shows what the model gets; no pictures in it

    approved = await _approve(
        db_session, org_and_owner, conversation, payload["pending_id"], _unchanged(payload)
    )

    answer = dict(approved)["assistant_message"]["content"]
    assert answer["image_matches"][0]["document_name"] == "lioness.png"
    assert "lioness" not in repr(model.calls)
    assert send_review is not None


async def test_the_non_streaming_answer_carries_them_too(
    client, db_session, org_and_owner, qdrant, text_answer, model, monkeypatch
):
    space = await _space(db_session, org_and_owner, "Animals")
    await _picture(db_session, org_and_owner, qdrant, space, BLUE, "lioness.png")
    conversation = await _conversation(db_session, org_and_owner)

    resp = await client.post(
        f"/api/v1/conversations/{conversation.id}/messages", json={"text": "动物母狮"}
    )

    assert resp.status_code == 201, resp.text
    content = resp.json()["assistant_message"]["content"]
    assert content["image_matches"][0]["document_name"] == "lioness.png"
    assert resp.json()["conversation"]["message_count"] == 2


# ---------------------------------------------------------------------------
# "Text only" (the person's choice)
# ---------------------------------------------------------------------------


async def test_text_only_adds_no_pictures_to_a_streamed_answer(
    db_session, org_and_owner, qdrant, text_answer, model
):
    space = await _space(db_session, org_and_owner, "Animals")
    await _picture(db_session, org_and_owner, qdrant, space, BLUE, "lioness.png")
    cid = (await _conversation(db_session, org_and_owner)).id

    events = await _stream(db_session, org_and_owner, cid, "动物母狮", with_pictures=False)

    answer = next(d for k, d in events if k == "assistant_message")["content"]
    assert "image_matches" not in answer
    audit = (
        await db_session.execute(
            select(AuditEvent.payload).where(AuditEvent.action == "message.sent")
        )
    ).scalars().all()[-1]
    assert "related_picture_ids" not in audit


async def test_text_only_over_http_skips_both_the_lookup_and_the_picture_shortcut(
    client, db_session, org_and_owner, qdrant, text_answer, model
):
    space = await _space(db_session, org_and_owner, "Animals")
    await _picture(db_session, org_and_owner, qdrant, space, BLUE, "lioness.png")
    conversation = await _conversation(db_session, org_and_owner)
    url = f"/api/v1/conversations/{conversation.id}/messages"

    # A plain request for pictures is answered from the pictures (no model) ...
    both = await client.post(url, json={"text": "有狮子的图片吗"})
    assert both.json()["assistant_message"]["content"]["meta"]["kind"] == "image_search"
    # ... but when the person chose text only, the same words take the ordinary text path.
    text_only = await client.post(url, json={"text": "有狮子的图片吗", "pictures": False})
    content = text_only.json()["assistant_message"]["content"]
    assert text_only.status_code == 201
    assert "image_matches" not in content
    assert content.get("meta", {}).get("kind") not in {"image_search", "image_list"}
    # A request that does not say anything about it behaves as before (pictures on).
    assert (await client.post(url, json={"text": "动物母狮"})).json()["assistant_message"][
        "content"
    ]["image_matches"]


async def test_a_held_prompt_remembers_that_the_person_chose_text_only(
    db_session, org_and_owner, qdrant, model, monkeypatch
):
    from tests.test_send_review import PASSAGE_A, _approve, _chunk, _unchanged

    space = await _space(db_session, org_and_owner, "Animals")
    await _picture(db_session, org_and_owner, qdrant, space, BLUE, "lioness.png")
    conversation = await _conversation(db_session, org_and_owner)

    async def fake_retrieve(*_a, **_k):
        return ScopedRetrieval(
            chunks=[_chunk(uuid.uuid4(), org_and_owner.personal_space_id, PASSAGE_A, 0)],
            space_ids=[org_and_owner.personal_space_id], scope_mode="auto", model_id="m",
            collection="c",
        )

    monkeypatch.setattr(conv, "retrieve_for_conversation", fake_retrieve)
    held = await _stream(
        db_session, org_and_owner, conversation.id, "动物母狮", review=True, with_pictures=False
    )
    payload = dict(held)["review_required"]

    approved = await _approve(
        db_session, org_and_owner, conversation, payload["pending_id"], _unchanged(payload)
    )

    assert "image_matches" not in dict(approved)["assistant_message"]["content"]


_ = types  # (kept for fixtures that build namespaces)
