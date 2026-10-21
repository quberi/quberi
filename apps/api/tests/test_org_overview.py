# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""The admin Overview: live counts, things needing attention, a trend, and the most cited documents.

The privacy rule is the one worth guarding: a document is named in "most cited" only when it
lives in an enterprise space or the admin could read it, however often it is cited.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select, update

from quberi_api.domain.conversations.models import Message
from quberi_api.domain.documents.models import Document
from quberi_api.domain.identity.models import User
from quberi_api.domain.spaces.models import Space
from quberi_api.services import conversations as conversations_service
from quberi_api.services import org_overview, space_bindings
from tests.test_document_access_explain import world  # noqa: F401
from tests.test_document_acl_api import _as, qdrant, web  # noqa: F401
from tests.test_external_save import _org, redis_client  # noqa: F401
from tests.test_image_indexing import _doc, _space
from tests.test_image_search import _grant
from tests.test_image_search import _member as _invite


def _cite(doc_id, used=True):
    return {"index": 1, "document_id": str(doc_id), "document_name": "x", "used": used}


async def _conversation(db, w, space_id, user_id):
    await db.commit()
    return (
        await conversations_service.create_conversation(
            db,
            space_id=space_id,
            organization_id=w.boot.organization_id,
            user_id=user_id,
            title="q",
        )
    ).conversation.id


async def _answers(db, w, conv, doc_ids, *, used=True, role="assistant", days_ago=0):
    created = datetime.now(timezone.utc) - timedelta(days=days_ago)
    for doc_id in doc_ids:
        db.add(
            Message(
                conversation_id=conv,
                organization_id=w.boot.organization_id,
                role=role,
                content={"text": "a", "citations": [_cite(doc_id, used)]},
                created_at=created,
            )
        )
    await db.commit()


async def _overview(db, w, actor):
    await db.commit()
    return await org_overview.build_overview(
        db, organization_id=w.boot.organization_id, actor_user_id=actor
    )


async def _admin(db, w):
    user = await _invite(db, w.boot, "admin")
    return SimpleNamespace(id=user.id)


async def _more_world(db, w):
    """An enterprise space and a colleague's private space, each with one document."""
    enterprise = await _space(db, w.boot, "Policies")
    await db.execute(
        update(Space)
        .where(Space.id == enterprise.id)
        .values(owner_type="organization", owner_id=w.boot.organization_id)
    )
    await db.commit()
    policy = await _doc(db, w.boot, enterprise, "policy.pdf", mime="application/pdf", state="indexed")
    colleague = await _invite(db, w.boot, "member")
    mine = await _space(db, w.boot, "Mine")
    await db.execute(update(Space).where(Space.id == mine.id).values(owner_id=colleague.id))
    await db.commit()
    private = await _doc(db, w.boot, mine, "private.pdf", mime="application/pdf", state="indexed")
    return SimpleNamespace(
        enterprise=SimpleNamespace(id=enterprise.id),
        policy=SimpleNamespace(id=policy.id),
        mine=SimpleNamespace(id=mine.id),
        private=SimpleNamespace(id=private.id),
        colleague=SimpleNamespace(id=colleague.id),
    )


# --------------------------------------------------------------------------
# Counts
# --------------------------------------------------------------------------


async def test_the_counts_match_the_tables(db_session, world):
    w = world
    o = await _overview(db_session, w, w.boot.user_id)

    all_users = (await db_session.execute(select(User.status).where(User.organization_id == w.boot.organization_id))).scalars().all()
    assert o.members.total == len(all_users)
    assert o.members.suspended == sum(1 for s in all_users if s in ("suspended", "archived"))
    assert o.members.active + o.members.invited + o.members.suspended == o.members.total

    docs = (
        await db_session.execute(
            select(Document.indexing_state).where(
                Document.organization_id == w.boot.organization_id, Document.status != "archived"
            )
        )
    ).scalars().all()
    assert o.documents.total == len(docs) == o.documents.indexed + o.documents.failed + o.documents.waiting
    assert o.spaces.total == o.spaces.enterprise + o.spaces.team + o.spaces.personal


async def test_an_enterprise_space_is_counted_apart_from_team_spaces(db_session, world):
    w = world
    before = await _overview(db_session, w, w.boot.user_id)
    await _more_world(db_session, w)

    after = await _overview(db_session, w, w.boot.user_id)

    assert after.spaces.enterprise == before.spaces.enterprise + 1
    assert after.spaces.team == before.spaces.team + 1  # "Mine" is still a team space


# --------------------------------------------------------------------------
# Needs attention
# --------------------------------------------------------------------------


def _attention(o, code):
    return next((a for a in o.attention if a.code == code), None)


async def test_documents_waiting_for_review_and_failed_indexing_are_flagged(db_session, world):
    w = world
    o = await _overview(db_session, w, w.boot.user_id)
    assert _attention(o, "pending_reviews").count == 1  # waiting.pdf
    assert _attention(o, "index_failed") is None

    failed = await _doc(db_session, w.boot, w.space, "bad.pdf", mime="application/pdf", state="failed", error="x")
    o = await _overview(db_session, w, w.boot.user_id)
    assert _attention(o, "index_failed").count == 1
    assert failed.id


async def test_a_space_open_to_the_whole_organisation_is_flagged_and_named_only_if_enterprise(
    db_session, world
):
    w = world
    extra = await _more_world(db_session, w)
    everyone = SimpleNamespace(id=w.boot.organization_id)
    for space in (extra.enterprise, extra.mine):
        await db_session.commit()
        await space_bindings.add_binding(
            db_session,
            space_id=space.id,
            organization_id=w.boot.organization_id,
            actor_user_id=w.boot.user_id,
            subject_type="organization",
            subject_id=everyone.id,
            role_code="viewer",
        )
        await db_session.commit()

    o = await _overview(db_session, w, w.boot.user_id)

    flagged = _attention(o, "org_wide_spaces")
    assert flagged.count == 2
    assert flagged.names == ("Policies",)  # the colleague's private space is counted, not named


async def test_a_deactivated_owner_who_still_holds_a_space_is_flagged(db_session, world):
    w = world
    extra = await _more_world(db_session, w)
    await db_session.execute(update(User).where(User.id == extra.colleague.id).values(status="suspended"))
    await db_session.commit()

    o = await _overview(db_session, w, w.boot.user_id)

    flagged = _attention(o, "suspended_owning_spaces")
    assert flagged is not None and flagged.count == 1


async def test_nothing_to_flag_means_no_items(db_session, org_and_owner):
    boot = org_and_owner
    await db_session.commit()
    o = await org_overview.build_overview(
        db_session, organization_id=boot.organization_id, actor_user_id=boot.user_id
    )
    assert o.attention == []


# --------------------------------------------------------------------------
# Trend
# --------------------------------------------------------------------------


async def test_the_trend_has_fourteen_days_with_today_last(db_session, world):
    w = world
    conv = await _conversation(db_session, w, w.space.id, w.viewer.id)
    await _answers(db_session, w, conv, [w.plain.id, w.plain.id], role="user")
    await _answers(db_session, w, conv, [w.plain.id], role="user", days_ago=3)

    o = await _overview(db_session, w, w.boot.user_id)

    assert len(o.trend) == 14
    days = [p.day for p in o.trend]
    assert days == sorted(days) and len(set(days)) == 14
    assert o.trend[-1].day == datetime.now(timezone.utc).date().isoformat()
    assert o.trend[-1].questions == 2
    assert o.trend[-4].questions == 1
    assert o.trend[-1].documents >= 3  # the world's documents were created today


async def test_days_follow_the_viewers_time_zone(db_session, world):
    w = world
    await db_session.commit()
    east = await org_overview.build_overview(
        db_session, organization_id=w.boot.organization_id, actor_user_id=w.boot.user_id,
        tz_offset_minutes=14 * 60,
    )
    west = await org_overview.build_overview(
        db_session, organization_id=w.boot.organization_id, actor_user_id=w.boot.user_id,
        tz_offset_minutes=-12 * 60,
    )
    # Twenty-six hours apart: the "today" label differs by a day or two.
    assert east.trend[-1].day != west.trend[-1].day


# --------------------------------------------------------------------------
# Most cited
# --------------------------------------------------------------------------


async def test_most_cited_counts_answers_not_chunks_and_ignores_unused_citations(db_session, world):
    w = world
    extra = await _more_world(db_session, w)
    conv = await _conversation(db_session, w, w.space.id, w.viewer.id)
    await _answers(db_session, w, conv, [extra.policy.id] * 3)
    await _answers(db_session, w, conv, [extra.policy.id] * 5, used=False)

    o = await _overview(db_session, w, w.boot.user_id)

    cited = {c.name: c for c in o.top_cited}
    assert cited["policy.pdf"].citations == 3
    assert cited["policy.pdf"].space_name == "Policies"


async def test_old_answers_and_deleted_documents_do_not_count(db_session, world):
    w = world
    extra = await _more_world(db_session, w)
    conv = await _conversation(db_session, w, w.space.id, w.viewer.id)
    await _answers(db_session, w, conv, [extra.policy.id], days_ago=45)
    await _answers(db_session, w, conv, [uuid.uuid4()] * 2)  # a document that no longer exists

    o = await _overview(db_session, w, w.boot.user_id)

    assert o.top_cited == []


async def test_a_private_document_is_never_named_however_often_it_is_cited(db_session, world):
    w = world
    extra = await _more_world(db_session, w)
    admin = await _admin(db_session, w)
    conv = await _conversation(db_session, w, w.space.id, w.viewer.id)
    await _answers(db_session, w, conv, [extra.private.id] * 5)
    await _answers(db_session, w, conv, [extra.policy.id] * 2)

    o = await _overview(db_session, w, admin.id)

    names = [c.name for c in o.top_cited]
    assert "private.pdf" not in names
    assert names == ["policy.pdf"]  # the enterprise document is named, as admins manage those


async def test_a_document_the_admin_can_read_is_named(db_session, world):
    w = world
    admin = await _admin(db_session, w)
    conv = await _conversation(db_session, w, w.space.id, w.viewer.id)
    await _answers(db_session, w, conv, [w.plain.id] * 2)

    before = await _overview(db_session, w, admin.id)
    assert before.top_cited == []

    await _grant(db_session, w.boot, w.space, admin, role="viewer")
    after = await _overview(db_session, w, admin.id)
    assert [(c.name, c.citations) for c in after.top_cited] == [("plain.pdf", 2)]


async def test_the_most_cited_come_first(db_session, world):
    w = world
    extra = await _more_world(db_session, w)
    admin = await _admin(db_session, w)
    await _grant(db_session, w.boot, w.space, admin, role="viewer")
    conv = await _conversation(db_session, w, w.space.id, w.viewer.id)
    await _answers(db_session, w, conv, [w.plain.id] * 2)
    await _answers(db_session, w, conv, [extra.policy.id] * 4)

    o = await _overview(db_session, w, admin.id)

    assert [(c.name, c.citations) for c in o.top_cited] == [("policy.pdf", 4), ("plain.pdf", 2)]


# --------------------------------------------------------------------------
# Over HTTP
# --------------------------------------------------------------------------


async def test_the_endpoint_is_for_owners_and_admins_only(web, db_session, world):
    from quberi_api.api.v1.organizations.overview import overview_router

    web._transport.app.include_router(overview_router, prefix="/api/v1")
    w = world
    await _as(web, db_session, w.boot, w.boot.user_id)

    ok = await web.get("/api/v1/organizations/current/overview", params={"tz_offset": 480})

    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert len(body["trend"]) == 14 and body["members"]["total"] >= 1
    assert {"documents", "spaces", "external", "attention", "top_cited", "cited_days"} <= set(body)

    await _as(web, db_session, w.boot, w.viewer.id)
    refused = await web.get("/api/v1/organizations/current/overview")
    assert refused.status_code == 403, refused.text
