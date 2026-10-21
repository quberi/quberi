# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""Why can this person read this document? The answer must match what they really see.

The explanation takes its verdict from the same functions the document doors use, and only
the reasons are gathered on the side. The matrix test below holds that to account: for every
person and every document it compares the explanation with the real read gate.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document
from quberi_api.domain.identity.models import User
from quberi_api.services import groups as groups_service
from quberi_api.services.documents import access_explain
from quberi_api.services.documents import core as documents_core
from quberi_api.services.documents.errors import DocumentPermissionError
from quberi_api.services.errors import NotFoundError
from tests.test_document_acl import _member, _rule
from tests.test_document_acl_api import _as, qdrant, web  # noqa: F401
from tests.test_external_save import _org, redis_client  # noqa: F401
from tests.test_image_indexing import _doc, _space
from tests.test_image_search import _grant


@pytest.fixture
async def world(db_session, org_and_owner):
    db, boot = db_session, org_and_owner
    space = SimpleNamespace(id=(await _space(db, boot, "Wiki")).id)

    async def person(role=None):
        who = await _member(db, boot)
        who = SimpleNamespace(id=who.id, display_name=who.display_name)
        if role:
            await _grant(db, boot, space, who, role=role)
        return who

    async def document(name):
        d = await _doc(db, boot, space, name, mime="application/pdf", state="indexed")
        return SimpleNamespace(id=d.id, name=name)

    viewer = await person("viewer")
    editor = await person("editor")
    space_owner = await person("owner")
    outsider = await person()
    shared = await person()
    suspended = await person("viewer")
    await db.execute(update(User).where(User.id == suspended.id).values(status="suspended"))
    await db.commit()

    plain = await document("plain.pdf")
    hidden = await document("hidden.pdf")
    waiting = await document("waiting.pdf")
    await db.execute(
        update(Document)
        .where(Document.id == waiting.id)
        .values(review_state="pending", uploaded_by_user_id=editor.id)
    )
    await db.commit()

    await _rule(db, boot, space, hidden, "deny", "user", viewer.id)
    await _rule(db, boot, space, plain, "allow", "user", shared.id)
    return SimpleNamespace(
        boot=boot, space=space, viewer=viewer, editor=editor, space_owner=space_owner,
        outsider=outsider, shared=shared, suspended=suspended,
        plain=plain, hidden=hidden, waiting=waiting,
    )


async def _explain(db, w, user, doc, actor=None):
    await db.commit()
    return await access_explain.explain_document_access(
        db, organization_id=w.boot.organization_id, actor_user_id=actor or w.boot.user_id,
        space_id=w.space.id, document_id=doc.id, subject_user_id=user.id,
    )


def _codes(result):
    return [s.code for s in result.steps]


async def _real_verdict(db, w, user, doc) -> bool:
    """What the document doors would say, asked the way the list and detail doors ask."""
    await db.commit()
    try:
        whole, access, review_scope = await documents_core._read_gate(
            db, user_id=user.id, organization_id=w.boot.organization_id, space_id=w.space.id
        )
        row = (await db.execute(select(Document).where(Document.id == doc.id))).scalar_one()
        documents_core._require_visible(
            row, whole_space=whole, access=access, review=review_scope
        )
    except NotFoundError:
        return False
    return True


# --------------------------------------------------------------------------
# The answer is the real answer
# --------------------------------------------------------------------------


async def test_the_explanation_agrees_with_the_real_read_gate_for_everyone_and_every_document(
    db_session, world
):
    w = world
    people = [w.viewer, w.editor, w.space_owner, w.outsider, w.shared, w.suspended]
    docs = [w.plain, w.hidden, w.waiting]
    seen = set()
    for person in people:
        for doc in docs:
            result = await _explain(db_session, w, person, doc)
            assert result.can_read == await _real_verdict(db_session, w, person, doc), (
                person.display_name, doc.name, result.outcome,
            )
            seen.add(result.outcome)
    # The matrix really does cover every kind of answer.
    assert seen == set(access_explain.OUTCOMES)


# --------------------------------------------------------------------------
# The reasons
# --------------------------------------------------------------------------


async def test_a_space_role_is_explained_with_the_path_it_came_through(db_session, world):
    result = await _explain(db_session, world, world.viewer, world.plain)

    assert result.can_read and result.outcome == "allowed_by_space"
    assert result.space_role == "viewer"
    role = next(s for s in result.steps if s.code == "space_role")
    assert role.effect == "grants" and role.role_code == "viewer" and role.hit_path == "direct"
    assert result.appears_in_search is True


async def test_a_person_with_no_role_and_no_share_has_no_access(db_session, world):
    result = await _explain(db_session, world, world.outsider, world.plain)

    assert not result.can_read and result.outcome == "no_access"
    assert result.space_role is None
    assert "no_space_role" in _codes(result)
    assert result.appears_in_search is False


async def test_an_allow_rule_shares_one_document_without_the_space(db_session, world):
    shared_doc = await _explain(db_session, world, world.shared, world.plain)
    other_doc = await _explain(db_session, world, world.shared, world.waiting)

    assert shared_doc.can_read and shared_doc.outcome == "allowed_by_share"
    allow = next(s for s in shared_doc.steps if s.code == "allow_rule")
    assert allow.effect == "grants" and allow.subject_type == "user" and allow.rule_id
    assert shared_doc.appears_in_search is True
    assert not other_doc.can_read and other_doc.outcome == "no_access"


async def test_a_deny_rule_wins_over_a_space_role_and_both_are_shown(db_session, world):
    result = await _explain(db_session, world, world.viewer, world.hidden)

    assert not result.can_read and result.outcome == "denied_by_rule"
    codes = _codes(result)
    assert "space_role" in codes and "deny_rule" in codes
    deny = next(s for s in result.steps if s.code == "deny_rule")
    assert deny.effect == "blocks" and "Deny wins" in deny.summary
    assert result.appears_in_search is False


async def test_a_deny_through_a_group_names_the_group(db_session, world):
    w, db = world, db_session
    await db.commit()
    group = await groups_service.create_group(
        db, organization_id=w.boot.organization_id, actor_user_id=w.boot.user_id,
        name="Contractors",
    )
    await db.commit()
    await groups_service.add_member(
        db, group_id=group.group.id, organization_id=w.boot.organization_id,
        actor_user_id=w.boot.user_id, member_type="user", member_id=w.editor.id,
    )
    await db.commit()
    await _rule(db, w.boot, w.space, w.plain, "deny", "group", group.group.id)

    result = await _explain(db, w, w.editor, w.plain)

    assert not result.can_read and result.outcome == "denied_by_rule"
    deny = next(s for s in result.steps if s.code == "deny_rule")
    assert deny.subject_type == "group" and deny.subject_display_name == "Contractors"


async def test_a_space_owner_is_not_denied_and_the_rule_is_shown_as_ignored(db_session, world):
    w = world
    await _rule(db_session, w.boot, w.space, w.plain, "deny", "organization", w.boot.organization_id)

    result = await _explain(db_session, w, w.space_owner, w.plain)

    assert result.can_read and result.outcome == "allowed_by_space"
    ignored = next(s for s in result.steps if s.code == "deny_ignored_for_owner")
    assert ignored.effect == "ignored"
    # Everyone else is hidden from it by the same rule.
    assert not (await _explain(db_session, w, w.editor, w.plain)).can_read


async def test_a_switched_off_account_reads_nothing_whatever_the_rules_say(db_session, world):
    w = world
    await _rule(db_session, w.boot, w.space, w.plain, "allow", "user", w.suspended.id)

    result = await _explain(db_session, w, w.suspended, w.plain)

    assert not result.can_read and result.outcome == "account_blocked"
    assert _codes(result)[0] == "account_blocked"
    # Nothing is listed as granting access to an account that cannot use it.
    assert all(s.effect != "grants" for s in result.steps)


async def test_a_file_waiting_for_approval_is_hidden_from_everyone_but_its_uploader(
    db_session, world
):
    w = world
    viewer = await _explain(db_session, w, w.viewer, w.waiting)
    uploader = await _explain(db_session, w, w.editor, w.waiting)

    assert not viewer.can_read and viewer.outcome == "waiting_for_approval"
    assert next(s for s in viewer.steps if s.code == "review_pending").effect == "blocks"
    assert uploader.can_read
    assert next(s for s in uploader.steps if s.code == "review_pending").effect == "info"
    # Readable by the uploader, but nobody can find it by searching yet.
    assert uploader.appears_in_search is False


async def test_readable_but_not_yet_indexed_is_called_out(db_session, world):
    w = world
    await db_session.execute(
        update(Document).where(Document.id == w.plain.id).values(indexing_state="indexing")
    )
    await db_session.commit()

    result = await _explain(db_session, w, w.viewer, w.plain)

    assert result.can_read and result.appears_in_search is False
    assert "not_searchable_yet" in _codes(result)


# --------------------------------------------------------------------------
# Who may ask
# --------------------------------------------------------------------------


async def test_only_someone_who_manages_the_space_may_ask(db_session, world):
    w = world
    for asker in (w.viewer, w.editor):
        with pytest.raises(DocumentPermissionError):
            await _explain(db_session, w, w.outsider, w.plain, actor=asker.id)


async def test_a_space_admin_may_ask(db_session, world):
    w = world
    admin = await _member(db_session, w.boot)
    await _grant(db_session, w.boot, w.space, SimpleNamespace(id=admin.id), role="admin")

    result = await _explain(db_session, w, w.viewer, w.plain, actor=admin.id)

    assert result.can_read


async def test_an_unknown_member_or_document_is_not_found(db_session, world):
    import uuid

    w = world
    with pytest.raises(NotFoundError):
        await _explain(db_session, w, SimpleNamespace(id=uuid.uuid4()), w.plain)
    with pytest.raises(NotFoundError):
        await _explain(db_session, w, w.viewer, SimpleNamespace(id=uuid.uuid4()))


async def test_a_member_of_another_organisation_is_not_found(db_session, world):
    w = world
    other_boot = await _org(db_session)
    stranger = await _member(db_session, other_boot)

    with pytest.raises(NotFoundError):
        await _explain(db_session, w, SimpleNamespace(id=stranger.id), w.plain)


async def test_asking_is_recorded_without_any_document_text(db_session, world):
    w = world
    await _explain(db_session, w, w.viewer, w.hidden)

    await db_session.commit()
    events = (
        await db_session.execute(
            select(AuditEvent).where(AuditEvent.action == "document.access_checked")
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].resource_id == w.hidden.id
    assert events[0].actor_id == w.boot.user_id
    assert events[0].payload == {"subject_user_id": str(w.viewer.id), "outcome": "denied_by_rule"}


# --------------------------------------------------------------------------
# Over HTTP
# --------------------------------------------------------------------------


def _url(w, doc):
    return f"/api/v1/spaces/{w.space.id}/documents/{doc.id}/access-check"


async def test_the_endpoint_returns_the_answer_and_the_steps(web, db_session, world):
    w = world
    await _as(web, db_session, w.boot, w.boot.user_id)

    resp = await web.get(_url(w, w.hidden), params={"user_id": str(w.viewer.id)})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["can_read"] is False and body["outcome"] == "denied_by_rule"
    assert body["document_name"] == "hidden.pdf" and body["user_display_name"]
    assert {s["code"] for s in body["steps"]} >= {"space_role", "deny_rule"}
    assert all({"kind", "effect", "code", "summary"} <= set(s) for s in body["steps"])
    assert "text" not in body  # names and rules, never document text


async def test_the_endpoint_refuses_someone_who_does_not_manage_the_space(
    web, db_session, world
):
    w = world
    await _as(web, db_session, w.boot, w.viewer.id)

    resp = await web.get(_url(w, w.hidden), params={"user_id": str(w.viewer.id)})

    assert resp.status_code == 403


async def test_the_endpoint_needs_a_member_and_404s_for_a_stranger(web, db_session, world):
    import uuid

    w = world
    await _as(web, db_session, w.boot, w.boot.user_id)

    missing = await web.get(_url(w, w.plain))
    unknown = await web.get(_url(w, w.plain), params={"user_id": str(uuid.uuid4())})

    assert missing.status_code == 422
    assert unknown.status_code == 404
