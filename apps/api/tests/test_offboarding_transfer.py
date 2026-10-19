# ruff: noqa: F811, E501  (fixtures are imported by name; some assertion lines run long)
"""The offboarding checklist and the batch handover of a departed member's spaces.

Real database, real permission evaluator, real sessions over HTTP. The checklist tells the
organisation owner what the member still holds; the handover moves their collaboration
spaces to a new owner, space by space, reporting each one.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update

from quberi_api.api import dependencies
from quberi_api.api.errors import register_exception_handlers
from quberi_api.api.v1.organizations import members as members_router
from quberi_api.api.v1.organizations.offboarding import offboarding_router
from quberi_api.core.config import get_settings
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.authz.models import Role, RoleBinding
from quberi_api.domain.documents.models import Document
from quberi_api.domain.identity.models import User
from quberi_api.domain.spaces.models import Space
from quberi_api.services import groups as groups_service
from quberi_api.services import offboarding as offboarding_service
from quberi_api.services import spaces as spaces_service
from quberi_api.services.documents import review
from tests.test_external_save import _org
from tests.test_image_indexing import _doc
from tests.test_member_deactivation import _login, _member, _owner_headers, _row


@pytest.fixture
async def client(db_session):
    app = FastAPI()
    app.state.settings = get_settings()
    app.include_router(members_router.router, prefix="/api/v1")
    app.include_router(offboarding_router, prefix="/api/v1")
    register_exception_handlers(app)

    async def _session():
        yield db_session

    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[dependencies.get_infrastructure] = lambda: SimpleNamespace()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _base(user_id):
    return f"/api/v1/organizations/current/members/{user_id}/offboarding"


async def _space(db, boot, creator_id, name):
    """A collaboration space whose owner is ``creator_id``."""
    await db.commit()
    view = await spaces_service.create_collaborative_space(
        db,
        organization_id=boot.organization_id,
        creator_user_id=creator_id,
        name=name,
        slug=f"s-{uuid.uuid4().hex[:8]}",
    )
    await db.commit()
    return SimpleNamespace(id=view.space.id, name=name)


async def _bind(db, boot, space_id, user_id, role_code, subject_type="user"):
    role = (
        await db.execute(
            select(Role).where(Role.code == role_code, Role.organization_id.is_(None))
        )
    ).scalar_one()
    db.add(
        RoleBinding(
            organization_id=boot.organization_id,
            role_id=role.id,
            subject_type=subject_type,
            subject_id=user_id,
            resource_type="space",
            resource_id=space_id,
            conditions={},
        )
    )
    await db.commit()


async def _pending_upload(db, boot, space, uploader_id, name="draft.pdf"):
    doc = await _doc(db, boot, space, name, mime="application/pdf")
    await db.execute(
        update(Document)
        .where(Document.id == doc.id)
        .values(review_state="pending", uploaded_by_user_id=uploader_id)
    )
    await db.commit()
    return doc.id


async def _suspend(db, user_id):
    await db.execute(update(User).where(User.id == user_id).values(status="suspended"))
    await db.commit()


async def _owner_of(db, space_id):
    await db.commit()
    owner_id = (await db.execute(select(Space.owner_id).where(Space.id == space_id))).scalar_one()
    bound = set(
        (
            await db.execute(
                select(RoleBinding.subject_id)
                .join(Role, Role.id == RoleBinding.role_id)
                .where(
                    RoleBinding.resource_id == space_id,
                    RoleBinding.resource_type == "space",
                    Role.code == "owner",
                )
            )
        )
        .scalars()
        .all()
    )
    await db.commit()
    return owner_id, bound


async def _audit_count(db, action, **payload):
    await db.commit()
    stmt = select(func.count()).select_from(AuditEvent).where(AuditEvent.action == action)
    for key, value in payload.items():
        stmt = stmt.where(AuditEvent.payload[key].astext == str(value))
    n = (await db.execute(stmt)).scalar_one()
    await db.commit()
    return n


async def _world(db):
    boot = await _org(db)
    leaver = await _member(db, boot, "member")
    heir = await _member(db, boot, "member")
    colleague = await _member(db, boot, "member")
    return boot, leaver, heir, colleague


# ---------------------------------------------------------------------------
# Checklist
# ---------------------------------------------------------------------------


async def test_the_checklist_lists_what_is_theirs_and_never_the_personal_space(client, db_session):
    boot, leaver, heir, colleague = await _world(db_session)
    alone = await _space(db_session, boot, leaver.id, "Alone")
    shared = await _space(db_session, boot, leaver.id, "Shared owners")
    await _bind(db_session, boot, shared.id, colleague.id, "owner")
    reviewing = await _space(db_session, boot, colleague.id, "Reviewing")
    await _bind(db_session, boot, reviewing.id, leaver.id, "admin")
    await _pending_upload(db_session, boot, alone, leaver.id, "a.pdf")
    await _pending_upload(db_session, boot, reviewing, leaver.id, "b.pdf")
    headers = await _owner_headers(db_session, boot)

    resp = await client.get(_base(leaver.id), headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["member"]["id"] == str(leaver.id) and body["member"]["status"] == "active"
    assert body["personal_space_status"] == "active"
    owned = {s["name"]: s for s in body["owned_spaces"]}
    assert set(owned) == {"Alone", "Shared owners"}  # the personal space is not here
    assert owned["Alone"]["needs_transfer"] is True and owned["Alone"]["other_owners"] == []
    assert owned["Alone"]["pending_review_count"] == 1
    assert owned["Shared owners"]["needs_transfer"] is False
    assert [o["user_id"] for o in owned["Shared owners"]["other_owners"]] == [str(colleague.id)]
    assert body["owned_spaces_total"] == 2 and body["needs_transfer_total"] == 1
    assert [(s["name"], s["other_reviewers"]) for s in body["reviewer_spaces"]] == [("Reviewing", 1)]
    uploads = {u["name"]: u for u in body["uploads_as_uploader"]}
    assert set(uploads) == {"a.pdf", "b.pdf"} and body["uploads_as_uploader_total"] == 2
    assert uploads["a.pdf"]["reviewer_count"] == 0  # he is the only reviewer there
    assert uploads["b.pdf"]["reviewer_count"] == 1
    assert body["impact"]["personal_spaces"] == 1
    assert body["impact"]["org_selections_referencing_private_keys"] == []
    assert not any("agent" in key for key in body)


async def test_the_checklist_lists_archived_spaces_with_their_status_and_not_frozen_ones(
    client, db_session
):
    boot, leaver, heir, colleague = await _world(db_session)
    live = await _space(db_session, boot, leaver.id, "Live")
    shelved = await _space(db_session, boot, leaver.id, "Shelved")
    frozen = await _space(db_session, boot, leaver.id, "Frozen")
    not_his = await _space(db_session, boot, colleague.id, "Not his")
    await db_session.execute(update(Space).where(Space.id == shelved.id).values(status="archived"))
    await db_session.execute(update(Space).where(Space.id == frozen.id).values(status="frozen"))
    await db_session.execute(update(Space).where(Space.id == not_his.id).values(status="archived"))
    await db_session.commit()

    body = (
        await client.get(_base(leaver.id), headers=await _owner_headers(db_session, boot))
    ).json()

    owned = {s["name"]: s for s in body["owned_spaces"]}
    assert set(owned) == {"Live", "Shelved"}
    assert (owned["Live"]["status"], owned["Shelved"]["status"]) == ("active", "archived")
    assert owned["Shelved"]["needs_transfer"] is True
    assert body["owned_spaces_total"] == 2 and body["needs_transfer_total"] == 2


async def test_an_archived_space_can_be_handed_over_by_owner_service_only_when_allowed(
    db_session,
):
    from quberi_api.services import space_owners
    from quberi_api.services.errors import InvalidInputError

    boot, leaver, heir, colleague = await _world(db_session)
    shelved = await _space(db_session, boot, leaver.id, "Shelved")
    await db_session.execute(update(Space).where(Space.id == shelved.id).values(status="archived"))
    await db_session.commit()
    args = dict(
        space_id=shelved.id,
        organization_id=boot.organization_id,
        actor_user_id=boot.user_id,
        new_owner_user_id=heir.id,
    )
    await _suspend(db_session, leaver.id)

    # Other callers keep the old rule: an archived space does not change owner.
    with pytest.raises(InvalidInputError):
        await space_owners.transfer_owner(db_session, **args)
    await db_session.rollback()
    assert (await _owner_of(db_session, shelved.id))[0] == leaver.id

    # A frozen space is refused even when archived ones are allowed.
    await db_session.execute(update(Space).where(Space.id == shelved.id).values(status="frozen"))
    await db_session.commit()
    with pytest.raises(InvalidInputError):
        await space_owners.transfer_owner(db_session, allow_archived=True, **args)
    await db_session.rollback()

    await db_session.execute(update(Space).where(Space.id == shelved.id).values(status="archived"))
    await db_session.commit()
    await space_owners.transfer_owner(db_session, allow_archived=True, **args)
    assert (await _owner_of(db_session, shelved.id))[0] == heir.id


async def test_a_group_that_holds_the_admin_role_counts_as_a_reviewer(client, db_session):
    boot, leaver, heir, colleague = await _world(db_session)
    lone = await _space(db_session, boot, leaver.id, "Lone")
    await _pending_upload(db_session, boot, lone, leaver.id)
    headers = await _owner_headers(db_session, boot)
    before = (await client.get(_base(leaver.id), headers=headers)).json()
    assert before["uploads_as_uploader"][0]["reviewer_count"] == 0

    await db_session.commit()
    group = await groups_service.create_group(
        db_session, organization_id=boot.organization_id, actor_user_id=boot.user_id, name="Leads"
    )
    await groups_service.add_member(
        db_session,
        group_id=group.group.id,
        member_type="user",
        member_id=colleague.id,
        organization_id=boot.organization_id,
        actor_user_id=boot.user_id,
    )
    await _bind(db_session, boot, lone.id, group.group.id, "admin", subject_type="group")

    after = (await client.get(_base(leaver.id), headers=headers)).json()
    assert after["uploads_as_uploader"][0]["reviewer_count"] == 1


async def test_a_reviewer_who_is_suspended_or_only_invited_does_not_count(client, db_session):
    boot, leaver, heir, colleague = await _world(db_session)
    lone = await _space(db_session, boot, leaver.id, "Lone")
    await _bind(db_session, boot, lone.id, colleague.id, "admin")
    await _bind(db_session, boot, lone.id, heir.id, "admin")
    await _pending_upload(db_session, boot, lone, leaver.id)
    await db_session.execute(update(User).where(User.id == colleague.id).values(status="suspended"))
    await db_session.execute(update(User).where(User.id == heir.id).values(status="invited"))
    await db_session.commit()
    body = (await client.get(_base(leaver.id), headers=await _owner_headers(db_session, boot))).json()
    assert body["uploads_as_uploader"][0]["reviewer_count"] == 0


async def test_spaces_that_need_a_new_owner_are_listed_first_and_totals_are_true(
    client, db_session, monkeypatch
):
    monkeypatch.setattr(offboarding_service, "MAX_OWNED_SPACES", 3)
    boot, leaver, heir, colleague = await _world(db_session)
    # Oldest first: four spaces that have a second owner, then two that do not.
    for i in range(4):
        space = await _space(db_session, boot, leaver.id, f"Shared {i}")
        await _bind(db_session, boot, space.id, colleague.id, "owner")
    for i in range(2):
        await _space(db_session, boot, leaver.id, f"Alone {i}")

    body = (
        await client.get(_base(leaver.id), headers=await _owner_headers(db_session, boot))
    ).json()

    assert body["owned_spaces_total"] == 6 and body["needs_transfer_total"] == 2
    assert len(body["owned_spaces"]) == 3
    assert [s["needs_transfer"] for s in body["owned_spaces"]] == [True, True, False]


async def test_the_checklist_shows_which_organisation_choices_use_a_private_key(client, db_session):
    from quberi_api.domain.ai.models import AiModelSelection, AiProviderBinding
    from quberi_api.services.security.crypto import seal

    boot, leaver, heir, colleague = await _world(db_session)
    binding = AiProviderBinding(
        identity_id=leaver.identity_id,
        provider_kind="openai_compat",
        display_name="mine",
        api_key_encrypted=seal("k"),
        status="active",
        is_platform_default=False,
    )
    db_session.add(binding)
    await db_session.flush()
    db_session.add(
        AiModelSelection(
            organization_id=boot.organization_id,
            purpose="embedding",
            provider_binding_id=binding.id,
            model_id="text-embedding-3-small",
        )
    )
    await db_session.commit()
    body = (
        await client.get(_base(leaver.id), headers=await _owner_headers(db_session, boot))
    ).json()
    assert body["impact"]["private_model_bindings"] == 1
    assert body["impact"]["org_selections_referencing_private_keys"] == ["embedding"]


async def test_the_checklist_is_for_the_organisation_owner_only(client, db_session):
    boot, leaver, heir, colleague = await _world(db_session)
    admin = await _member(db_session, boot, "admin")
    other = await _org(db_session)
    for headers in (await _login(db_session, boot, admin), await _login(db_session, boot, heir)):
        assert (await client.get(_base(leaver.id), headers=headers)).status_code == 403
    assert (
        await client.get(_base(leaver.id), headers=await _owner_headers(db_session, other))
    ).status_code == 404
    assert (
        await client.get(_base(uuid.uuid4()), headers=await _owner_headers(db_session, boot))
    ).status_code == 404
    await db_session.execute(update(User).where(User.id == leaver.id).values(status="archived"))
    await db_session.commit()
    gone = await client.get(_base(leaver.id), headers=await _owner_headers(db_session, boot))
    assert gone.status_code == 409 and gone.json()["code"] == "member_archived"


# ---------------------------------------------------------------------------
# Handover
# ---------------------------------------------------------------------------


async def _transfer(client, db_session, boot, leaver, new_owner_id, space_ids):
    return await client.post(
        f"{_base(leaver.id)}/transfer-spaces",
        headers=await _owner_headers(db_session, boot),
        json={"new_owner_user_id": str(new_owner_id), "space_ids": [str(s) for s in space_ids]},
    )


async def test_a_batch_reports_every_space_and_hands_over_the_ones_that_can_go(client, db_session):
    boot, leaver, heir, colleague = await _world(db_session)
    first = await _space(db_session, boot, leaver.id, "First")
    second = await _space(db_session, boot, leaver.id, "Second")
    with_other = await _space(db_session, boot, leaver.id, "With another owner")
    await _bind(db_session, boot, with_other.id, colleague.id, "owner")
    not_his = await _space(db_session, boot, colleague.id, "Not his")
    archived = await _space(db_session, boot, leaver.id, "Archived")
    await db_session.execute(update(Space).where(Space.id == archived.id).values(status="archived"))
    # A collaboration space in a state that is not readable (frozen) is not handed over.
    frozen = await _space(db_session, boot, leaver.id, "Frozen")
    await db_session.execute(update(Space).where(Space.id == frozen.id).values(status="frozen"))
    personal = (
        await db_session.execute(
            select(Space.id).where(Space.owner_id == leaver.id, Space.preset == "personal")
        )
    ).scalar_one()
    await _pending_upload(db_session, boot, first, uploader_id=colleague.id)
    await _suspend(db_session, leaver.id)
    ghost = uuid.uuid4()

    resp = await _transfer(
        client,
        db_session,
        boot,
        leaver,
        heir.id,
        [first.id, second.id, with_other.id, not_his.id, archived.id, frozen.id, personal, ghost],
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_space = {r["space_id"]: r for r in body["results"]}
    assert [r["space_id"] for r in body["results"]] == [
        str(s)
        for s in [first.id, second.id, with_other.id, not_his.id, archived.id, frozen.id, personal, ghost]
    ]
    assert by_space[str(first.id)] == {
        "space_id": str(first.id),
        "status": "transferred",
        "code": None,
        "pending_review_count": 1,
    }
    assert by_space[str(second.id)]["status"] == "transferred"
    assert (by_space[str(with_other.id)]["status"], by_space[str(with_other.id)]["code"]) == (
        "skipped",
        "has_other_owner",
    )
    assert by_space[str(not_his.id)]["code"] == "not_owned_by_member"
    assert by_space[str(archived.id)]["status"] == "transferred"
    assert (by_space[str(frozen.id)]["status"], by_space[str(frozen.id)]["code"]) == (
        "skipped",
        "space_not_active",
    )
    assert by_space[str(personal)]["code"] == "personal_space"
    assert (by_space[str(ghost)]["status"], by_space[str(ghost)]["code"]) == (
        "failed",
        "space_not_found",
    )
    assert (body["transferred"], body["already_done"], body["skipped"], body["failed"]) == (3, 0, 4, 1)

    for space in (first, second, archived):
        owner_id, bound = await _owner_of(db_session, space.id)
        assert owner_id == heir.id and bound == {heir.id}

    # The archived space is handed over and stays archived; the frozen one is untouched.
    await db_session.commit()
    statuses = dict(
        (
            await db_session.execute(
                select(Space.id, Space.status).where(Space.id.in_([archived.id, frozen.id]))
            )
        ).all()
    )
    assert statuses == {archived.id: "archived", frozen.id: "frozen"}
    assert (await _owner_of(db_session, frozen.id))[0] == leaver.id
    # The ones that were skipped are untouched, the personal space included.
    assert (await _owner_of(db_session, personal))[0] == leaver.id
    assert leaver.id in (await _owner_of(db_session, with_other.id))[1]
    # The new owner now has the pending uploads in their review inbox.
    inbox = await review.pending_inbox(
        db_session, organization_id=boot.organization_id, user_id=heir.id
    )
    await db_session.commit()
    assert [p.space_id for p in inbox] == [first.id]
    # Audit: one event per handed-over space, plus the summary.
    assert await _audit_count(
        db_session, "space.owner_transferred", offboarded_user_id=leaver.id
    ) == 3
    assert await _audit_count(db_session, "member.spaces_transferred", transferred=3) == 1


async def test_repeating_a_finished_request_changes_nothing_and_adds_no_audit(client, db_session):
    boot, leaver, heir, colleague = await _world(db_session)
    first = await _space(db_session, boot, leaver.id, "First")
    not_his = await _space(db_session, boot, colleague.id, "Not his")
    # The heir owns this one too, but never through the departed member.
    await _bind(db_session, boot, not_his.id, heir.id, "owner")
    await _suspend(db_session, leaver.id)

    one = (await _transfer(client, db_session, boot, leaver, heir.id, [first.id, not_his.id])).json()
    two = (await _transfer(client, db_session, boot, leaver, heir.id, [first.id, not_his.id])).json()

    assert one["results"][0]["status"] == "transferred"
    assert two["results"][0] == {
        "space_id": str(first.id),
        "status": "already_done",
        "code": None,
        "pending_review_count": 0,
    }
    # Looking like a finished handover is not enough: it must really have been one.
    assert (two["results"][1]["status"], two["results"][1]["code"]) == (
        "skipped",
        "not_owned_by_member",
    )
    assert (two["transferred"], two["already_done"], two["skipped"]) == (0, 1, 1)
    assert await _audit_count(db_session, "space.owner_transferred", offboarded_user_id=leaver.id) == 1
    assert await _audit_count(db_session, "member.spaces_transferred") == 1


async def test_duplicates_in_the_request_are_handled_once(client, db_session):
    boot, leaver, heir, colleague = await _world(db_session)
    first = await _space(db_session, boot, leaver.id, "First")
    await _suspend(db_session, leaver.id)
    body = (
        await _transfer(client, db_session, boot, leaver, heir.id, [first.id, first.id, first.id])
    ).json()
    assert len(body["results"]) == 1 and body["transferred"] == 1


async def test_a_request_that_is_not_acceptable_does_nothing_at_all(client, db_session):
    boot, leaver, heir, colleague = await _world(db_session)
    owned = await _space(db_session, boot, leaver.id, "Owned")
    invited = await _member(db_session, boot, "member")
    await db_session.execute(update(User).where(User.id == invited.id).values(status="invited"))
    await db_session.commit()

    # The member is still active: deactivate first.
    resp = await _transfer(client, db_session, boot, leaver, heir.id, [owned.id])
    assert resp.status_code == 409 and resp.json()["code"] == "member_not_suspended"
    await _suspend(db_session, leaver.id)

    cases = [
        (invited.id, [owned.id], "new_owner_not_active"),
        (leaver.id, [owned.id], "new_owner_is_departed_member"),
        (uuid.uuid4(), [owned.id], "new_owner_not_found"),
        (heir.id, [], "no_spaces"),
        (heir.id, [uuid.uuid4() for _ in range(101)], "batch_too_large"),
    ]
    for new_owner, spaces, code in cases:
        resp = await _transfer(client, db_session, boot, leaver, new_owner, spaces)
        assert resp.status_code == 400, (code, resp.text)
        assert resp.json()["code"] == code
    # More than 500 entries is refused before anything is read.
    resp = await _transfer(
        client, db_session, boot, leaver, heir.id, [uuid.uuid4() for _ in range(501)]
    )
    assert resp.status_code == 422
    # Nothing moved, nothing was audited.
    assert (await _owner_of(db_session, owned.id))[0] == leaver.id
    assert await _audit_count(db_session, "space.owner_transferred") == 0
    assert await _audit_count(db_session, "member.spaces_transferred") == 0


async def test_the_organisation_owner_cannot_be_the_new_owner(client, db_session, monkeypatch):
    from quberi_api.services import access_guard
    from tests.conftest import _REAL_IS_RESTRICTED_ORG

    boot, leaver, heir, colleague = await _world(db_session)
    owned = await _space(db_session, boot, leaver.id, "Owned")
    await _suspend(db_session, leaver.id)
    monkeypatch.setattr(access_guard, "is_restricted_org", _REAL_IS_RESTRICTED_ORG)
    resp = await _transfer(client, db_session, boot, leaver, boot.user_id, [owned.id])
    assert resp.status_code == 400 and resp.json()["code"] == "new_owner_is_org_owner"
    assert (await _owner_of(db_session, owned.id))[0] == leaver.id


async def test_a_hundred_spaces_pass_the_size_limit_once_duplicates_are_removed(
    client, db_session
):
    boot, leaver, heir, colleague = await _world(db_session)
    await _suspend(db_session, leaver.id)
    ids = [uuid.uuid4() for _ in range(100)]
    resp = await _transfer(client, db_session, boot, leaver, heir.id, ids + ids[:50])
    assert resp.status_code == 200
    assert resp.json()["failed"] == 100  # none of them exist, each reported on its own


async def test_only_the_organisation_owner_may_hand_spaces_over(client, db_session):
    boot, leaver, heir, colleague = await _world(db_session)
    admin = await _member(db_session, boot, "admin")
    owned = await _space(db_session, boot, leaver.id, "Owned")
    other = await _org(db_session)
    await _suspend(db_session, leaver.id)
    payload = {"new_owner_user_id": str(heir.id), "space_ids": [str(owned.id)]}
    url = f"{_base(leaver.id)}/transfer-spaces"
    for headers in (await _login(db_session, boot, admin), await _login(db_session, boot, heir)):
        assert (await client.post(url, headers=headers, json=payload)).status_code == 403
    outsider = await _owner_headers(db_session, other)
    assert (await client.post(url, headers=outsider, json=payload)).status_code == 404
    assert (await client.post(url, json=payload)).status_code == 401
    assert (await _owner_of(db_session, owned.id))[0] == leaver.id


async def test_a_space_that_fails_is_reported_and_the_others_still_go(
    client, db_session, monkeypatch
):
    from quberi_api.services.errors import ForbiddenError

    boot, leaver, heir, colleague = await _world(db_session)
    bad = await _space(db_session, boot, leaver.id, "Bad")
    good = await _space(db_session, boot, leaver.id, "Good")
    await _suspend(db_session, leaver.id)
    real = offboarding_service.transfer_owner

    async def flaky(session, **kw):
        if kw["space_id"] == bad.id:
            raise ForbiddenError("no")
        return await real(session, **kw)

    monkeypatch.setattr(offboarding_service, "transfer_owner", flaky)
    body = (await _transfer(client, db_session, boot, leaver, heir.id, [bad.id, good.id])).json()
    assert [(r["status"], r["code"]) for r in body["results"]] == [
        ("failed", "not_allowed"),
        ("transferred", None),
    ]
    assert (await _owner_of(db_session, bad.id))[0] == leaver.id
    assert (await _owner_of(db_session, good.id))[0] == heir.id
    assert await _audit_count(db_session, "member.spaces_transferred", transferred=1, failed=1) == 1


async def test_the_whole_flow_deactivate_then_hand_over_then_look_again(client, db_session):
    boot, leaver, heir, colleague = await _world(db_session)
    owned = await _space(db_session, boot, leaver.id, "Owned")
    headers = await _owner_headers(db_session, boot)
    before = (await client.get(_base(leaver.id), headers=headers)).json()
    assert before["needs_transfer_total"] == 1

    deact = await client.post(
        f"/api/v1/organizations/current/members/{leaver.id}/deactivate", headers=headers
    )
    assert deact.status_code == 200
    assert (await _row(db_session, leaver.id)).status == "suspended"
    mid = (await client.get(_base(leaver.id), headers=headers)).json()
    assert mid["member"]["status"] == "suspended" and mid["personal_space_status"] == "frozen"
    assert mid["needs_transfer_total"] == 1

    done = await _transfer(client, db_session, boot, leaver, heir.id, [owned.id])
    assert done.status_code == 200
    after = (await client.get(_base(leaver.id), headers=headers)).json()
    assert after["owned_spaces_total"] == 0 and after["needs_transfer_total"] == 0
