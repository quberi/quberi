"""Picture questions typed into the ordinary chat box, over HTTP.

"库里有哪些图片" and "有风景画吗" are answered from the picture index: no model, no free
allowance spent, no "check before sending" hold. Everything else still goes the text way.
The caller, database, permission evaluator and picture store are real (qdrant in memory);
the image model is a stand-in.
"""

from __future__ import annotations

import json
from datetime import timedelta
import uuid

import pytest
from sqlalchemy import select, update

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.conversations.models import Message
from quberi_api.domain.documents.models import Document
from quberi_api.services import conversations as conversations_service
from quberi_api.services import usage as usage_service
from tests.test_image_search import BLUE, RED
from tests.test_image_search_api import (  # noqa: F401  (fixtures)
    _conversation,
    _picture,
    client,
    fake_embedder,
    qdrant,
    redis_client,
)
from tests.test_image_indexing import _doc, _space


@pytest.fixture
def quota_spy(monkeypatch):
    """Records free-allowance checks; a picture question must make none."""
    calls: list[str] = []

    async def spy(session, *, organization_id, tier):
        calls.append("quota")

    monkeypatch.setattr(usage_service, "enforce_free_quota", spy)
    return calls


def _events(resp) -> list[tuple[str, dict]]:
    out = []
    for block in resp.text.split("\n\n"):
        if not block.strip():
            continue
        kind = next(l[7:] for l in block.splitlines() if l.startswith("event: "))
        data = next(l[6:] for l in block.splitlines() if l.startswith("data: "))
        out.append((kind, json.loads(data)))
    return out


async def _ask(http, conv, text, *, review=False):
    return await http.post(
        f"/api/v1/conversations/{conv.id}/messages/stream", json={"text": text, "review": review}
    )


async def test_which_pictures_are_there_lists_them_without_the_review_hold(
    client, db_session, org_and_owner, qdrant, quota_spy
):
    space = await _space(db_session, org_and_owner, "Scenery")
    await _picture(db_session, org_and_owner, qdrant, space, RED, "old.png")
    await _picture(db_session, org_and_owner, qdrant, space, BLUE, "new.png")
    conv = await _conversation(db_session, org_and_owner)

    resp = await _ask(client, conv, "库里有哪些图片", review=True)

    assert resp.status_code == 200, resp.text
    events = _events(resp)
    assert [k for k, _ in events] == ["user_message", "assistant_message", "done"]
    answer = events[1][1]["content"]
    assert answer["text"] == "知识库里共有 2 张图片。"
    assert {m["document_name"] for m in answer["image_matches"]} == {"old.png", "new.png"}
    assert all("score" not in m for m in answer["image_matches"])
    assert answer["meta"]["kind"] == "image_list"
    assert events[0][1]["content"] == {"type": "text", "text": "库里有哪些图片"}
    assert quota_spy == []  # no model, no allowance


async def test_a_description_finds_the_closest_pictures(
    client, db_session, org_and_owner, qdrant, quota_spy
):
    space = await _space(db_session, org_and_owner, "Scenery")
    await _picture(db_session, org_and_owner, qdrant, space, BLUE, "sea.png")
    await _picture(db_session, org_and_owner, qdrant, space, RED, "sunset.png")
    conv = await _conversation(db_session, org_and_owner)

    resp = await _ask(client, conv, "有风景画吗")  # no colour word: the stand-in says blue

    answer = _events(resp)[1][1]["content"]
    assert answer["image_matches"][0]["document_name"] == "sea.png"
    assert answer["image_matches"][0]["score"] > answer["image_matches"][1]["score"]
    assert answer["text"] == "找到 2 张相似的图片。"
    assert answer["meta"]["kind"] == "image_search"
    assert quota_spy == []


async def test_a_weak_best_match_is_not_presented_as_a_find(
    client, db_session, org_and_owner, qdrant
):
    space = await _space(db_session, org_and_owner, "Scenery")
    await _picture(db_session, org_and_owner, qdrant, space, RED, "red.png")
    conv = await _conversation(db_session, org_and_owner)

    resp = await _ask(client, conv, "有没有蓝色的图片")  # query points at blue, picture is red

    text = _events(resp)[1][1]["content"]["text"]
    assert "没有明显吻合" in text and "请自行判断" in text


async def test_the_non_streaming_endpoint_answers_the_same_way(
    client, db_session, org_and_owner, qdrant, quota_spy
):
    space = await _space(db_session, org_and_owner, "Scenery")
    await _picture(db_session, org_and_owner, qdrant, space, RED, "a.png")
    conv = await _conversation(db_session, org_and_owner)

    resp = await client.post(f"/api/v1/conversations/{conv.id}/messages", json={"text": "有什么图片"})

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["assistant_message"]["content"]["image_matches"][0]["document_name"] == "a.png"
    assert body["conversation"]["message_count"] == 2
    assert quota_spy == []


async def test_the_list_skips_archived_pictures_and_pictures_outside_the_scope(
    client, db_session, org_and_owner, qdrant
):
    mine = await _space(db_session, org_and_owner, "Mine")
    other = await _space(db_session, org_and_owner, "Other")
    keep = await _picture(db_session, org_and_owner, qdrant, mine, RED, "keep.png")
    gone = await _picture(db_session, org_and_owner, qdrant, mine, RED, "gone.png")
    await _picture(db_session, org_and_owner, qdrant, other, RED, "elsewhere.png")
    await db_session.execute(update(Document).where(Document.id == gone.id).values(status="archived"))
    await db_session.commit()
    conv = await _conversation(db_session, org_and_owner)
    await conversations_service.set_scope_mode(
        db_session, conversation_id=conv.id, organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id, mode="custom",
    )
    await conversations_service.remove_scope_source(
        db_session, conversation_id=conv.id, organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id, space_id=other.id,
    )

    resp = await _ask(client, conv, "有哪些图片")

    names = [m["document_name"] for m in _events(resp)[1][1]["content"]["image_matches"]]
    assert names == ["keep.png"]
    assert str(keep.id) in repr(_events(resp))  # sanity: the id is the live one


async def test_the_list_is_newest_first_and_capped(
    client, db_session, org_and_owner, qdrant
):
    space = await _space(db_session, org_and_owner, "Many")
    for i in range(32):
        await _doc(db_session, org_and_owner, space, f"p{i:02d}.png", state="indexed", age=timedelta(minutes=40 - i))
    conv = await _conversation(db_session, org_and_owner)

    resp = await _ask(client, conv, "列出所有图片")

    content = _events(resp)[1][1]["content"]
    assert len(content["image_matches"]) == 30
    assert content["image_matches"][0]["document_name"] == "p31.png"
    assert content["text"] == "知识库里共有 32 张图片，先列出最新的 30 张。"


async def test_someone_without_access_to_the_pictures_gets_the_text_path(
    client, db_session, org_and_owner, qdrant
):
    from tests.test_image_search import _member

    hidden = await _space(db_session, org_and_owner, "Hidden")
    await _picture(db_session, org_and_owner, qdrant, hidden, RED, "hidden.png")
    member = await _member(db_session, org_and_owner)
    personal = (
        await db_session.execute(
            select(conversations_service.Space.id).where(
                conversations_service.Space.owner_id == member.id,
                conversations_service.Space.preset == "personal",
            )
        )
    ).scalar_one()
    await db_session.commit()
    conv = (
        await conversations_service.create_conversation(
            db_session, space_id=personal, organization_id=org_and_owner.organization_id,
            user_id=member.id, title=None,
        )
    ).conversation
    await db_session.commit()

    plan = await conversations_service.plan_picture_turn(
        db_session, conversation_id=conv.id, organization_id=org_and_owner.organization_id,
        user_id=member.id, text="库里有哪些图片",
    )

    assert plan is None  # nothing they may see: the normal path answers, hidden.png stays hidden


async def test_without_any_picture_the_question_goes_the_normal_way(
    db_session, org_and_owner, qdrant
):
    await _space(db_session, org_and_owner, "Docs only")
    conv = await _conversation(db_session, org_and_owner)

    plan = await conversations_service.plan_picture_turn(
        db_session, conversation_id=conv.id, organization_id=org_and_owner.organization_id,
        user_id=org_and_owner.user_id, text="库里有哪些图片",
    )

    assert plan is None


@pytest.mark.parametrize("text", ["怎么压缩图片", "这份文档里有图片吗", "总结一下合同", "has the roadmap changed?"])
async def test_ordinary_questions_are_left_alone(db_session, org_and_owner, qdrant, text):
    space = await _space(db_session, org_and_owner, "Scenery")
    await _picture(db_session, org_and_owner, qdrant, space, RED, "a.png")
    conv = await _conversation(db_session, org_and_owner)

    plan = await conversations_service.plan_picture_turn(
        db_session, conversation_id=conv.id, organization_id=org_and_owner.organization_id,
        user_id=org_and_owner.user_id, text=text,
    )

    assert plan is None


async def test_a_conversation_bound_to_an_agent_keeps_its_agent(
    db_session, org_and_owner, qdrant, monkeypatch
):
    space = await _space(db_session, org_and_owner, "Scenery")
    await _picture(db_session, org_and_owner, qdrant, space, RED, "a.png")
    conv = await _conversation(db_session, org_and_owner)
    monkeypatch.setattr(conversations_service, "_agent_binding", lambda _c: uuid.uuid4())

    plan = await conversations_service.plan_picture_turn(
        db_session, conversation_id=conv.id, organization_id=org_and_owner.organization_id,
        user_id=org_and_owner.user_id, text="库里有哪些图片",
    )

    assert plan is None


async def test_picture_search_switched_off_leaves_the_text_path(
    db_session, org_and_owner, qdrant, monkeypatch
):
    space = await _space(db_session, org_and_owner, "Scenery")
    await _picture(db_session, org_and_owner, qdrant, space, RED, "a.png")
    conv = await _conversation(db_session, org_and_owner)
    monkeypatch.setattr(
        conversations_service, "get_settings",
        lambda: type("S", (), {"image_search_enabled": False})(),
    )

    plan = await conversations_service.plan_picture_turn(
        db_session, conversation_id=conv.id, organization_id=org_and_owner.organization_id,
        user_id=org_and_owner.user_id, text="库里有哪些图片",
    )

    assert plan is None


async def test_the_turn_is_saved_counted_and_audited_with_ids_only(
    client, db_session, org_and_owner, qdrant
):
    space = await _space(db_session, org_and_owner, "Scenery")
    pic = await _picture(db_session, org_and_owner, qdrant, space, RED, "private-name.png")
    conv = await _conversation(db_session, org_and_owner)

    await _ask(client, conv, "有哪些图片")

    rows = (
        await db_session.execute(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at)
        )
    ).scalars().all()
    assert [m.role for m in rows] == ["user", "assistant"]
    assert rows[1].cost == {"amount": 0, "currency": "USD"}
    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.action == "message.sent", AuditEvent.resource_id == conv.id
            )
        )
    ).scalar_one()
    assert event.payload["kind"] == "image_list"
    assert event.payload["model_id"] is None
    assert event.payload["retrieved_document_ids"] == [str(pic.id)]
    assert "private-name.png" not in repr(event.payload)
    assert "有哪些图片" not in repr(event.payload)

