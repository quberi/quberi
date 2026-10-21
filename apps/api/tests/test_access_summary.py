# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""The "who can see this" cards must count what people really experience.

The counts come from set-wise shortcuts and from the real read gate; these tests hold both
to a brute-force count that asks the document doors about every person in the organisation.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from quberi_api.domain.identity.models import User
from quberi_api.services import access_summary
from quberi_api.services.documents import core as documents_core
from quberi_api.services.documents.errors import DocumentPermissionError
from tests.test_document_access_explain import _real_verdict, world  # noqa: F401
from tests.test_document_acl import _rule
from tests.test_document_acl_api import _as, qdrant, web  # noqa: F401
from tests.test_external_save import _org, redis_client  # noqa: F401
from tests.test_image_indexing import _doc


async def _everyone(db, w):
    await db.commit()
    return (
        await db.execute(select(User.id).where(User.organization_id == w.boot.organization_id))
    ).scalars().all()


async def _brute_force_readers(db, w, doc) -> int:
    n = 0
    for user_id in await _everyone(db, w):
        if await _real_verdict(db, w, SimpleNamespace(id=user_id), doc):
            n += 1
    return n


async def _doc_summary(db, w, doc, actor=None):
    await db.commit()
    return await access_summary.document_summary(
        db,
        organization_id=w.boot.organization_id,
        actor_user_id=actor or w.boot.user_id,
        space_id=w.space.id,
        document_id=doc.id,
    )


async def test_the_space_count_is_everyone_who_can_read_it_as_a_whole(db_session, world):
    w = world
    await db_session.commit()
    summary = await access_summary.space_summary(
        db_session,
        organization_id=w.boot.organization_id,
        actor_user_id=w.boot.user_id,
        space_id=w.space.id,
    )

    expected = 0
    for user_id in await _everyone(db_session, w):
        role = await documents_core._caller_space_role(
            db_session, user_id=user_id, organization_id=w.boot.organization_id, space_id=w.space.id
        )
        expected += 1 if documents_core._at_least(role, "viewer") else 0
    assert summary.people == expected
    # The suspended viewer is bound to the space but cannot read anything, so is not counted.
    assert sum(summary.by_role.values()) == summary.people
    assert summary.by_role.get("owner", 0) >= 1 and summary.by_role.get("viewer", 0) >= 1


async def test_the_space_card_names_who_it_is_open_to_and_counts_the_exceptions(db_session, world):
    w = world
    await db_session.commit()
    summary = await access_summary.space_summary(
        db_session,
        organization_id=w.boot.organization_id,
        actor_user_id=w.boot.user_id,
        space_id=w.space.id,
    )

    assert {s.subject_type for s in summary.sources} == {"user"}
    assert {s.role_code for s in summary.sources} >= {"viewer", "editor", "owner"}
    assert summary.allow_rules == 1 and summary.deny_rules == 1
    # plain.pdf and hidden.pdf carry a rule; waiting.pdf is not approved so it is not counted.
    assert summary.documents == 2
    assert summary.documents_with_exceptions == 2


async def test_a_document_without_rules_is_counted_like_its_space(db_session, world):
    w = world
    bare = await _doc(db_session, w.boot, w.space, "bare.pdf", mime="application/pdf", state="indexed")
    bare = SimpleNamespace(id=bare.id)

    summary = await _doc_summary(db_session, w, bare)

    assert summary.allow_rules == 0 and summary.deny_rules == 0
    assert summary.readers == summary.space_people
    assert summary.readers == await _brute_force_readers(db_session, w, bare)


async def test_a_document_with_rules_agrees_with_the_real_read_gate(db_session, world):
    w = world
    for doc in (w.plain, w.hidden):
        summary = await _doc_summary(db_session, w, doc)
        assert summary.readers == await _brute_force_readers(db_session, w, doc), doc.name
        # The pieces add up: the space's readers, minus those a rule hides it from,
        # plus those it is shared with on its own.
        assert summary.readers == summary.space_people - summary.hidden + summary.shared_only


async def test_sharing_and_hiding_show_up_as_separate_numbers(db_session, world):
    w = world
    shared = await _doc_summary(db_session, w, w.plain)
    hidden = await _doc_summary(db_session, w, w.hidden)

    assert (shared.allow_rules, shared.deny_rules) == (1, 0)
    assert shared.shared_only == 1 and shared.hidden == 0
    assert (hidden.allow_rules, hidden.deny_rules) == (0, 1)
    assert hidden.hidden == 1 and hidden.shared_only == 0
    assert hidden.readers == hidden.space_people - 1


async def test_a_document_still_waiting_for_approval_has_no_reader_count(db_session, world):
    summary = await _doc_summary(db_session, world, world.waiting)

    assert summary.review_state == "pending"
    assert summary.readers is None


async def test_only_a_manager_can_ask_about_a_document(db_session, world):
    with pytest.raises(DocumentPermissionError):
        await _doc_summary(db_session, world, world.plain, actor=world.viewer.id)


async def test_a_rule_that_changes_who_can_read_changes_the_count(db_session, world):
    w = world
    before = await _doc_summary(db_session, w, w.plain)

    await _rule(db_session, w.boot, w.space, w.plain, "deny", "user", w.editor.id)
    after = await _doc_summary(db_session, w, w.plain)

    assert after.deny_rules == 1
    assert after.hidden == 1
    assert after.readers == before.readers - 1
    assert after.readers == await _brute_force_readers(db_session, w, w.plain)


# --------------------------------------------------------------------------
# Over HTTP
# --------------------------------------------------------------------------


async def test_the_space_endpoint_returns_the_card(web, db_session, world):
    from quberi_api.api.v1.spaces.access import space_access_router

    # The shared test app does not mount the space-access routes; add them for this test.
    web._transport.app.include_router(space_access_router, prefix="/api/v1")
    w = world
    await _as(web, db_session, w.boot, w.space_owner.id)

    resp = await web.get(f"/api/v1/spaces/{w.space.id}/access-summary")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["people"] >= 3 and body["documents"] == 2
    assert body["allow_rules"] == 1 and body["deny_rules"] == 1
    assert body["sources"] and {"subject_type", "name", "role_code"} <= set(body["sources"][0])


async def test_the_document_endpoint_returns_the_card_to_a_manager_only(web, db_session, world):
    w = world
    url = f"/api/v1/spaces/{w.space.id}/documents/{w.hidden.id}/access-summary"
    await _as(web, db_session, w.boot, w.boot.user_id)

    ok = await web.get(url)

    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["deny_rules"] == 1 and body["hidden"] == 1
    assert body["readers"] == body["space_people"] - 1

    await _as(web, db_session, w.boot, w.viewer.id)
    refused = await web.get(url)
    assert refused.status_code == 403, refused.text
