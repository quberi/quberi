"""An administrator asks a space's owner for access; the owner decides."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, update

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.identity.models import User
from quberi_api.services import access_requests as svc
from quberi_api.services.errors import (
    AlreadyExistsError,
    ForbiddenError,
    InvalidInputError,
    NotFoundError,
)
from tests.test_document_acl_api import _as, qdrant, web  # noqa: F401
from tests.test_external_save import redis_client  # noqa: F401
from tests.test_image_search import fake_embedder  # noqa: F401
from tests.test_super_admin_rules import (  # noqa: F401
    _bind,
    _enterprise_space,
    _person,
    _role,
    _world,
)

pytestmark = pytest.mark.usefixtures("strict_rules")


async def _ask(db, w, who, role="viewer", reason="need to audit", space=None):
    await db.commit()
    view = await svc.create_request(
        db, organization_id=w.boot.organization_id, requester_user_id=who.id,
        space_id=(space or w.space).id, role_code=role, reason=reason,
    )
    await db.commit()
    return view.request.id


async def _decide(db, w, actor, request_id, approve=True, note=""):
    await db.commit()
    view = await svc.decide(
        db, organization_id=w.boot.organization_id, actor_user_id=actor.id,
        request_id=request_id, approve=approve, note=note,
    )
    await db.commit()
    return view


async def test_an_admin_asks_and_the_space_owner_approves(db_session, org_and_owner):
    db, boot = db_session, org_and_owner
    w = await _world(db, boot)
    assert await _role(db, w, w.space, w.other_admin) is None
    request_id = await _ask(db, w, w.other_admin, "editor", "check the Q3 contracts")

    await db.commit()
    pending = await svc.list_to_decide(db, organization_id=boot.organization_id, user_id=w.admin.id)
    assert [(v.request.id, v.requester_name, v.space_name, v.request.role_code, v.request.reason)
            for v in pending] == [
        (request_id, w.other_admin.name, "Tech", "editor", "check the Q3 contracts")
    ]
    # Nobody else sees it: not the requester, not another admin, not the super administrator.
    for other in (w.other_admin, w.member, w.owner):
        await db.commit()
        assert await svc.list_to_decide(db, organization_id=boot.organization_id, user_id=other.id) == []

    view = await _decide(db, w, w.admin, request_id, True, "ok until Friday")
    assert view.request.status == "approved" and view.decided_by_name == w.admin.name
    assert await _role(db, w, w.space, w.other_admin) == "editor"
    actions = (
        await db.execute(select(AuditEvent.action).where(AuditEvent.action.like("space.access_request%")))
    ).scalars().all()
    # (the test runs in one long transaction, so created_at cannot order them)
    assert sorted(actions) == ["space.access_request_approved", "space.access_requested"]
    await db.commit()
    assert await svc.list_to_decide(db, organization_id=boot.organization_id, user_id=w.admin.id) == []


async def test_a_rejection_grants_nothing_and_they_may_ask_again(db_session, org_and_owner):
    db, boot = db_session, org_and_owner
    w = await _world(db, boot)
    request_id = await _ask(db, w, w.other_admin)
    view = await _decide(db, w, w.admin, request_id, False, "not this quarter")
    assert view.request.status == "rejected" and view.request.decision_note == "not this quarter"
    assert await _role(db, w, w.space, w.other_admin) is None
    again = await _ask(db, w, w.other_admin, reason="new reason")
    assert again != request_id


async def test_the_super_administrator_can_ask_too_but_never_decides(db_session, org_and_owner):
    db, boot = db_session, org_and_owner
    w = await _world(db, boot)
    request_id = await _ask(db, w, w.owner, "viewer", "annual review")
    other_id = await _ask(db, w, w.other_admin, "viewer", "audit")
    await db.commit()
    with pytest.raises(ForbiddenError):  # their own request
        await svc.decide(db, organization_id=boot.organization_id, actor_user_id=w.owner.id,
                         request_id=request_id, approve=True)
    await db.rollback()
    with pytest.raises(NotFoundError):  # somebody else's: not an owner of the space
        await svc.decide(db, organization_id=boot.organization_id, actor_user_id=w.owner.id,
                         request_id=other_id, approve=True)
    await db.rollback()
    with pytest.raises(NotFoundError):  # another admin is no owner either
        await svc.decide(db, organization_id=boot.organization_id, actor_user_id=w.other_admin.id,
                         request_id=request_id, approve=True)
    await db.rollback()
    await _decide(db, w, w.admin, request_id)
    assert await _role(db, w, w.space, w.owner) == "viewer"


async def test_who_may_ask_and_for_what(db_session, org_and_owner):
    db, boot = db_session, org_and_owner
    w = await _world(db, boot)
    await db.commit()

    with pytest.raises(ForbiddenError):  # an ordinary member
        await svc.create_request(db, organization_id=boot.organization_id, requester_user_id=w.member.id,
                                 space_id=w.space.id, role_code="viewer", reason="x")
    await db.rollback()
    for role in ("admin", "owner"):  # only viewer or editor can be asked for
        with pytest.raises(InvalidInputError):
            await svc.create_request(db, organization_id=boot.organization_id, requester_user_id=w.other_admin.id,
                                     space_id=w.space.id, role_code=role, reason="x")
        await db.rollback()
    for reason in ("", "   ", "x" * 501):
        with pytest.raises(InvalidInputError):
            await svc.create_request(db, organization_id=boot.organization_id, requester_user_id=w.other_admin.id,
                                     space_id=w.space.id, role_code="viewer", reason=reason)
        await db.rollback()
    with pytest.raises(NotFoundError):  # a personal space is not an enterprise space
        await svc.create_request(db, organization_id=boot.organization_id, requester_user_id=w.other_admin.id,
                                 space_id=boot.personal_space_id, role_code="viewer", reason="x")
    await db.rollback()

    await _ask(db, w, w.other_admin)
    await db.commit()
    with pytest.raises(AlreadyExistsError):  # one pending request at a time
        await svc.create_request(db, organization_id=boot.organization_id, requester_user_id=w.other_admin.id,
                                 space_id=w.space.id, role_code="editor", reason="more")
    await db.rollback()


async def test_nothing_to_ask_for_when_you_already_have_it_or_nobody_can_answer(
    db_session, org_and_owner
):
    db, boot = db_session, org_and_owner
    w = await _world(db, boot)
    await _bind(db, w, w.space, w.other_admin, "editor")
    await db.commit()
    for role in ("viewer", "editor"):
        with pytest.raises(InvalidInputError):
            await svc.create_request(db, organization_id=boot.organization_id, requester_user_id=w.other_admin.id,
                                     space_id=w.space.id, role_code=role, reason="x")
        await db.rollback()

    other = await _enterprise_space(db, boot, w.admin, "HR")
    await db.execute(update(User).where(User.id == w.admin.id).values(status="suspended"))
    await db.commit()
    with pytest.raises(InvalidInputError):  # the only owner has left
        await svc.create_request(db, organization_id=boot.organization_id, requester_user_id=w.owner.id,
                                 space_id=other.id, role_code="viewer", reason="x")
    await db.rollback()


async def test_only_the_requester_can_withdraw_and_a_decided_request_is_final(
    db_session, org_and_owner
):
    db, boot = db_session, org_and_owner
    w = await _world(db, boot)
    request_id = await _ask(db, w, w.other_admin)
    await db.commit()
    with pytest.raises(NotFoundError):
        await svc.cancel(db, organization_id=boot.organization_id, actor_user_id=w.admin.id, request_id=request_id)
    await db.rollback()
    await db.commit()
    with pytest.raises(ForbiddenError):  # you cannot decide your own request
        await svc.decide(db, organization_id=boot.organization_id, actor_user_id=w.other_admin.id,
                         request_id=request_id, approve=True)
    await db.rollback()
    await db.commit()
    done = await svc.cancel(db, organization_id=boot.organization_id, actor_user_id=w.other_admin.id,
                            request_id=request_id)
    await db.commit()
    assert done.request.status == "cancelled"
    with pytest.raises(InvalidInputError):
        await svc.decide(db, organization_id=boot.organization_id, actor_user_id=w.admin.id,
                         request_id=request_id, approve=True)
    await db.rollback()
    mine = await svc.list_mine(db, organization_id=boot.organization_id, user_id=w.other_admin.id)
    assert [v.request.status for v in mine] == ["cancelled"]


async def test_the_http_flow_and_the_admin_console_hints(web, db_session, org_and_owner):
    db, boot = db_session, org_and_owner
    w = await _world(db, boot)
    sid = str(w.space.id)

    await _as(web, db, boot, w.other_admin.id)
    listing = (await web.get("/api/v1/spaces/organization")).json()
    mine = next(s for s in listing["items"] if s["id"] == sid)
    assert mine["caller_can_read"] is False and mine["caller_pending_request_id"] is None

    await _as(web, db, boot, w.other_admin.id)
    created = await web.post("/api/v1/access-requests", json={"space_id": sid, "role": "viewer", "reason": "audit"})
    assert created.status_code == 201, created.text
    rid = created.json()["id"]
    await _as(web, db, boot, w.other_admin.id)
    assert (await web.post("/api/v1/access-requests", json={"space_id": sid, "role": "viewer", "reason": "again"})).status_code == 409
    await _as(web, db, boot, w.other_admin.id)
    listing = (await web.get("/api/v1/spaces/organization")).json()
    assert next(s for s in listing["items"] if s["id"] == sid)["caller_pending_request_id"] == rid

    await _as(web, db, boot, w.member.id)
    assert (await web.post("/api/v1/access-requests", json={"space_id": sid, "role": "viewer", "reason": "x"})).status_code == 403

    await _as(web, db, boot, w.admin.id)
    todo = (await web.get("/api/v1/access-requests")).json()
    assert [i["id"] for i in todo["items"]] == [rid] and todo["items"][0]["reason"] == "audit"
    await _as(web, db, boot, w.other_admin.id)
    assert (await web.post(f"/api/v1/access-requests/{rid}/approve", json={})).status_code == 403
    await _as(web, db, boot, w.admin.id)
    ok = await web.post(f"/api/v1/access-requests/{rid}/approve", json={"note": "fine"})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"

    await _as(web, db, boot, w.other_admin.id)
    listing = (await web.get("/api/v1/spaces/organization")).json()
    assert next(s for s in listing["items"] if s["id"] == sid)["caller_can_read"] is True
    await _as(web, db, boot, w.other_admin.id)
    history = (await web.get("/api/v1/access-requests", params={"scope": "mine"})).json()
    assert history["items"][0]["status"] == "approved" and history["items"][0]["decision_note"] == "fine"
