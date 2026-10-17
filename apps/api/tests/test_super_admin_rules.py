"""The organisation owner is the super administrator, and nobody gives themselves access.

Run with the rules switched on (``strict_rules``); most other tests predate them and treat
the organisation owner as an ordinary user.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.authz.models import Role, RoleBinding
from quberi_api.domain.identity.models import User
from quberi_api.domain.spaces.models import Space
from quberi_api.services import access_guard, groups as groups_service
from quberi_api.services import org_units as units_service
from quberi_api.services import space_bindings as bindings_service
from quberi_api.services import space_owners
from quberi_api.services import spaces as spaces_service
from quberi_api.services.documents import access_rules as rules
from quberi_api.services.documents import core as documents_core
from quberi_api.services.errors import ForbiddenError, InvalidInputError, NotFoundError
from tests.test_image_search import _member as _invite

pytestmark = pytest.mark.usefixtures("strict_rules")


async def _person(db, boot, role="member"):
    user = await _invite(db, boot, role)
    return SimpleNamespace(id=user.id, name=user.display_name)


async def _enterprise_space(db, boot, creator, name="Tech"):
    await db.commit()
    view = await spaces_service.create_collaborative_space(
        db,
        organization_id=boot.organization_id,
        creator_user_id=creator.id,
        name=name,
        slug=f"s-{uuid.uuid4().hex[:8]}",
        tier="team",
        owner="organization",
    )
    await db.commit()
    return SimpleNamespace(id=view.space.id)


async def _world(db, boot):
    """A super admin (the org owner), an admin who owns the space, a second admin, a member."""
    owner = SimpleNamespace(id=boot.user_id)
    admin = await _person(db, boot, "admin")
    other_admin = await _person(db, boot, "admin")
    member = await _person(db, boot)
    space = await _enterprise_space(db, boot, admin)
    return SimpleNamespace(boot=boot, owner=owner, admin=admin, other_admin=other_admin,
                           member=member, space=space)


async def _bind(db, w, space, who, role="viewer", actor=None):
    await db.commit()
    await bindings_service.add_binding(
        db, space_id=space.id, organization_id=w.boot.organization_id,
        actor_user_id=(actor or w.admin).id, subject_type="user", subject_id=who.id, role_code=role,
    )
    await db.commit()


async def _role(db, w, space, who):
    await db.commit()
    role = await documents_core._caller_space_role(
        db, user_id=who.id, organization_id=w.boot.organization_id, space_id=space.id
    )
    await db.commit()
    return role


# ---------------------------------------------------------------------------
# The organisation owner does not create spaces or administer them
# ---------------------------------------------------------------------------


async def test_the_org_owner_cannot_create_a_space_but_an_admin_can(db_session, org_and_owner):
    db, boot = db_session, org_and_owner
    admin = await _person(db, boot, "admin")
    await db.commit()
    for owner_kind in ("user", "organization"):
        with pytest.raises(ForbiddenError):
            await spaces_service.create_collaborative_space(
                db, organization_id=boot.organization_id, creator_user_id=boot.user_id,
                name="X", slug=f"x-{uuid.uuid4().hex[:6]}", tier="team", owner=owner_kind,
            )
        await db.rollback()
    for owner_kind in ("user", "organization"):
        await spaces_service.create_collaborative_space(
            db, organization_id=boot.organization_id, creator_user_id=admin.id,
            name="Y", slug=f"y-{uuid.uuid4().hex[:6]}", tier="team", owner=owner_kind,
        )
        await db.commit()


async def test_a_personal_organisation_owner_is_unrestricted(db_session):
    from quberi_api.services import onboarding

    unique = uuid.uuid4().hex[:8]
    boot = await onboarding.bootstrap_organization(
        db_session, name=f"Solo {unique}", slug=f"solo-{unique}",
        owner_email=f"solo-{unique}@example.com", owner_display_name="Solo", tier="personal",
    )
    await db_session.commit()
    view = await spaces_service.create_collaborative_space(
        db_session, organization_id=boot.organization_id, creator_user_id=boot.user_id,
        name="Mine", slug=f"m-{unique}", tier="personal",
    )
    await db_session.commit()
    me = SimpleNamespace(id=boot.user_id)
    role = await documents_core._caller_space_role(
        db_session, user_id=me.id, organization_id=boot.organization_id, space_id=view.space.id
    )
    assert role == "owner"


async def test_the_org_owner_cannot_be_made_owner_or_admin_of_a_space(db_session, org_and_owner):
    db, boot = db_session, org_and_owner
    w = await _world(db, boot)
    for role in ("owner", "admin"):
        await db.commit()
        with pytest.raises(ForbiddenError):
            await bindings_service.add_binding(
                db, space_id=w.space.id, organization_id=boot.organization_id,
                actor_user_id=w.admin.id, subject_type="user", subject_id=w.owner.id, role_code=role,
            )
        await db.rollback()
    # Viewer and editor are fine when the space owner grants them.
    await _bind(db, w, w.space, w.owner, "editor")
    assert await _role(db, w, w.space, w.owner) == "editor"


async def test_a_leftover_admin_grant_to_the_org_owner_counts_only_as_editor(
    db_session, org_and_owner
):
    db, boot = db_session, org_and_owner
    w = await _world(db, boot)
    role = (
        await db.execute(select(Role).where(Role.code == "owner", Role.is_system.is_(True)))
    ).scalar_one()
    db.add(
        RoleBinding(
            organization_id=boot.organization_id, role_id=role.id, subject_type="user",
            subject_id=w.owner.id, resource_type="space", resource_id=w.space.id, conditions={},
        )
    )
    await db.commit()
    assert await _role(db, w, w.space, w.owner) == "editor"  # clamped
    assert await _role(db, w, w.space, w.admin) == "owner"   # others are unaffected
    # ...and a grant via a group that carries the admin role is clamped too.
    await db.commit()
    assert await _role(db, w, w.space, w.member) is None


# ---------------------------------------------------------------------------
# Nobody gives themselves access
# ---------------------------------------------------------------------------


async def test_an_admin_cannot_grant_themselves_a_space_they_do_not_own(db_session, org_and_owner):
    db, boot = db_session, org_and_owner
    w = await _world(db, boot)
    await db.commit()
    with pytest.raises(ForbiddenError):
        await bindings_service.add_binding(
            db, space_id=w.space.id, organization_id=boot.organization_id,
            actor_user_id=w.other_admin.id, subject_type="user",
            subject_id=w.other_admin.id, role_code="viewer",
        )
    await db.rollback()
    # Granting somebody else is fine, and so is the owner granting an admin.
    await _bind(db, w, w.space, w.member, "viewer", actor=w.other_admin)
    await _bind(db, w, w.space, w.other_admin, "viewer", actor=w.admin)
    assert await _role(db, w, w.space, w.other_admin) == "viewer"


async def test_the_indirect_routes_are_closed_too(db_session, org_and_owner):
    db, boot = db_session, org_and_owner
    w = await _world(db, boot)
    await db.commit()
    group = await groups_service.create_group(
        db, organization_id=boot.organization_id, actor_user_id=w.other_admin.id, name="Everyone-ish"
    )
    gid = group.group.id
    await db.commit()
    # A group the space is shared with (set up by the owner).
    await db.commit()
    await bindings_service.add_binding(
        db, space_id=w.space.id, organization_id=boot.organization_id,
        actor_user_id=w.admin.id, subject_type="group", subject_id=gid, role_code="viewer",
    )
    await db.commit()

    # Putting yourself in that group is a way in: refused.
    with pytest.raises(ForbiddenError):
        await groups_service.add_member(
            db, group_id=gid, organization_id=boot.organization_id,
            actor_user_id=w.other_admin.id, member_type="user", member_id=w.other_admin.id,
        )
    await db.rollback()
    # Putting somebody else in is not.
    await db.commit()
    await groups_service.add_member(
        db, group_id=gid, organization_id=boot.organization_id,
        actor_user_id=w.other_admin.id, member_type="user", member_id=w.member.id,
    )
    await db.commit()

    # The same through an organisation unit, and by sharing with the whole organisation.
    unit = await units_service.create_org_unit(
        db, organization_id=boot.organization_id, actor_user_id=w.other_admin.id, name="Ops", type="department"
    )
    uid = unit.unit.id
    await db.commit()
    await bindings_service.add_binding(
        db, space_id=w.space.id, organization_id=boot.organization_id,
        actor_user_id=w.admin.id, subject_type="organization_unit", subject_id=uid, role_code="viewer",
    )
    await db.commit()
    with pytest.raises(ForbiddenError):
        await units_service.assign_user_to_unit(
            db, unit_id=uid, user_id=w.other_admin.id, organization_id=boot.organization_id,
            actor_user_id=w.other_admin.id,
        )
    await db.rollback()
    await db.commit()
    with pytest.raises(ForbiddenError):  # the whole organisation includes the person asking
        await bindings_service.add_binding(
            db, space_id=w.space.id, organization_id=boot.organization_id,
            actor_user_id=w.other_admin.id, subject_type="organization",
            subject_id=boot.organization_id, role_code="viewer",
        )
    await db.rollback()


async def test_the_super_administrator_is_held_to_the_same_rule(db_session, org_and_owner):
    db, boot = db_session, org_and_owner
    w = await _world(db, boot)
    await db.commit()
    with pytest.raises(ForbiddenError):
        await bindings_service.add_binding(
            db, space_id=w.space.id, organization_id=boot.organization_id,
            actor_user_id=w.owner.id, subject_type="user", subject_id=w.owner.id, role_code="viewer",
        )
    await db.rollback()
    with pytest.raises(ForbiddenError):  # nor via everyone in the organisation
        await bindings_service.add_binding(
            db, space_id=w.space.id, organization_id=boot.organization_id,
            actor_user_id=w.owner.id, subject_type="organization",
            subject_id=boot.organization_id, role_code="viewer",
        )
    await db.rollback()
    # What the super administrator may do: manage access for other people.
    await _bind(db, w, w.space, w.member, "viewer", actor=w.owner)


async def test_document_rules_cannot_be_used_to_reach_in_or_to_lift_a_denial(
    db_session, org_and_owner
):
    from tests.test_image_indexing import _doc as make_doc

    db, boot = db_session, org_and_owner
    w = await _world(db, boot)
    space_row = await db.get(Space, w.space.id)
    doc = await make_doc(db, boot, space_row, "plan.pdf", mime="application/pdf", state="indexed")
    doc_id = doc.id
    await db.commit()
    # An admin who cannot read the space shares a document with themselves: refused.
    with pytest.raises(ForbiddenError):
        await rules.add_rule(
            db, organization_id=boot.organization_id, actor_user_id=w.other_admin.id,
            space_id=w.space.id, document_id=doc_id, effect="allow",
            subject_type="user", subject_id=w.other_admin.id,
        )
    await db.rollback()
    # The space owner hides it from them, and they cannot lift that themselves.
    await db.commit()
    hidden = await rules.add_rule(
        db, organization_id=boot.organization_id, actor_user_id=w.admin.id,
        space_id=w.space.id, document_id=doc_id, effect="deny",
        subject_type="user", subject_id=w.other_admin.id,
    )
    hidden_id = hidden.rule.id
    await db.commit()
    await _bind(db, w, w.space, w.other_admin, "viewer", actor=w.admin)
    await db.commit()
    with pytest.raises(ForbiddenError):
        await rules.remove_rule(
            db, organization_id=boot.organization_id, actor_user_id=w.other_admin.id,
            space_id=w.space.id, document_id=doc_id, rule_id=hidden_id,
        )
    await db.rollback()
    # The owner of the space can, since the document is theirs to decide.
    await db.commit()
    await rules.remove_rule(
        db, organization_id=boot.organization_id, actor_user_id=w.admin.id,
        space_id=w.space.id, document_id=doc_id, rule_id=hidden_id,
    )


# ---------------------------------------------------------------------------
# Handing a space over
# ---------------------------------------------------------------------------


async def _transfer(db, w, actor, new_owner, space=None):
    await db.commit()
    result = await space_owners.transfer_owner(
        db, space_id=(space or w.space).id, organization_id=w.boot.organization_id,
        actor_user_id=actor.id, new_owner_user_id=new_owner.id,
    )
    await db.commit()
    return result


async def test_an_owner_can_hand_the_space_over_and_keeps_editing(db_session, org_and_owner):
    db, boot = db_session, org_and_owner
    w = await _world(db, boot)
    result = await _transfer(db, w, w.admin, w.member)
    assert result.mode == "handover"
    assert await _role(db, w, w.space, w.member) == "owner"
    assert await _role(db, w, w.space, w.admin) == "editor"
    events = (
        await db.execute(select(AuditEvent.payload).where(AuditEvent.action == "space.owner_transferred"))
    ).scalars().all()
    assert events[-1]["mode"] == "handover" and events[-1]["new_owner_user_id"] == str(w.member.id)


async def test_nobody_else_can_hand_over_a_space_that_has_an_owner(db_session, org_and_owner):
    db, boot = db_session, org_and_owner
    w = await _world(db, boot)
    for actor in (w.owner, w.other_admin, w.member):
        await db.commit()
        with pytest.raises(ForbiddenError):
            await space_owners.transfer_owner(
                db, space_id=w.space.id, organization_id=boot.organization_id,
                actor_user_id=actor.id, new_owner_user_id=w.member.id,
            )
        await db.rollback()


async def test_when_the_owner_has_left_the_super_administrator_names_a_new_one(
    db_session, org_and_owner
):
    db, boot = db_session, org_and_owner
    w = await _world(db, boot)
    await db.execute(update(User).where(User.id == w.admin.id).values(status="suspended"))
    await db.commit()
    assert await space_owners.owners_unavailable_for_spaces(
        db, organization_id=boot.organization_id, space_ids=[w.space.id]
    ) == {w.space.id}

    # An admin who is not the super administrator cannot step in.
    await db.commit()
    with pytest.raises(ForbiddenError):
        await space_owners.transfer_owner(
            db, space_id=w.space.id, organization_id=boot.organization_id,
            actor_user_id=w.other_admin.id, new_owner_user_id=w.other_admin.id,
        )
    await db.rollback()
    # The super administrator cannot name themselves ...
    await db.commit()
    with pytest.raises(ForbiddenError):
        await space_owners.transfer_owner(
            db, space_id=w.space.id, organization_id=boot.organization_id,
            actor_user_id=w.owner.id, new_owner_user_id=w.owner.id,
        )
    await db.rollback()
    # ... or someone who has not joined yet or is not in the organisation.
    invited = await _person(db, boot)
    await db.execute(update(User).where(User.id == invited.id).values(status="invited"))
    await db.commit()
    with pytest.raises(InvalidInputError):
        await space_owners.transfer_owner(
            db, space_id=w.space.id, organization_id=boot.organization_id,
            actor_user_id=w.owner.id, new_owner_user_id=invited.id,
        )
    await db.rollback()
    await db.commit()
    with pytest.raises(NotFoundError):
        await space_owners.transfer_owner(
            db, space_id=w.space.id, organization_id=boot.organization_id,
            actor_user_id=w.owner.id, new_owner_user_id=uuid.uuid4(),
        )
    await db.rollback()

    result = await _transfer(db, w, w.owner, w.other_admin)
    assert result.mode == "replacement" and result.removed_owner_user_ids == [w.admin.id]
    assert await _role(db, w, w.space, w.other_admin) == "owner"
    assert await _role(db, w, w.space, w.admin) is None  # the old owner lost the role
    assert await _role(db, w, w.space, w.owner) is None  # and the super administrator gained nothing
    assert await space_owners.owners_unavailable_for_spaces(
        db, organization_id=boot.organization_id, space_ids=[w.space.id]
    ) == set()


async def test_a_space_held_only_by_the_org_owner_counts_as_ownerless(db_session, org_and_owner):
    """What the first deployment looks like: the org owner created the space and still holds it."""
    db, boot = db_session, org_and_owner
    w = await _world(db, boot)
    role = (
        await db.execute(select(Role).where(Role.code == "owner", Role.is_system.is_(True)))
    ).scalar_one()
    await db.execute(
        RoleBinding.__table__.delete().where(
            RoleBinding.resource_id == w.space.id, RoleBinding.role_id == role.id
        )
    )
    db.add(
        RoleBinding(
            organization_id=boot.organization_id, role_id=role.id, subject_type="user",
            subject_id=w.owner.id, resource_type="space", resource_id=w.space.id, conditions={},
        )
    )
    await db.commit()
    assert await space_owners.owners_unavailable_for_spaces(
        db, organization_id=boot.organization_id, space_ids=[w.space.id]
    ) == {w.space.id}
    result = await _transfer(db, w, w.owner, w.member)
    assert result.mode == "replacement"
    assert await _role(db, w, w.space, w.member) == "owner"
    bindings = (
        await db.execute(
            select(RoleBinding.subject_id).where(
                RoleBinding.resource_id == w.space.id, RoleBinding.role_id == role.id
            )
        )
    ).scalars().all()
    assert bindings == [w.member.id]  # the leftover grant to the super administrator is gone


async def test_a_personal_space_cannot_change_owner(db_session, org_and_owner):
    db, boot = db_session, org_and_owner
    w = await _world(db, boot)
    await db.commit()
    with pytest.raises(InvalidInputError):
        await space_owners.transfer_owner(
            db, space_id=boot.personal_space_id, organization_id=boot.organization_id,
            actor_user_id=w.owner.id, new_owner_user_id=w.member.id,
        )
    await db.rollback()


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------

from tests.test_document_acl_api import _as, qdrant, web  # noqa: E402,F401
from tests.test_image_search import fake_embedder  # noqa: E402,F401
from tests.test_external_save import redis_client  # noqa: E402,F401


async def test_the_admin_console_flags_ownerless_spaces_and_the_owner_can_fix_them(
    web, db_session, org_and_owner
):
    db, boot = db_session, org_and_owner
    w = await _world(db, boot)
    await db.execute(update(User).where(User.id == w.admin.id).values(status="suspended"))
    await db.commit()

    await _as(web, db, boot, w.owner.id)
    listing = (await web.get("/api/v1/spaces/organization")).json()
    mine = next(s for s in listing["items"] if s["id"] == str(w.space.id))
    assert mine["owner_unavailable"] is True

    # An ordinary member and an admin who is not the super administrator are refused.
    await _as(web, db, boot, w.member.id)
    url = f"/api/v1/spaces/{w.space.id}/owner"
    assert (await web.post(url, json={"new_owner_user_id": str(w.member.id)})).status_code == 403
    await _as(web, db, boot, w.other_admin.id)
    assert (await web.post(url, json={"new_owner_user_id": str(w.other_admin.id)})).status_code == 403

    await _as(web, db, boot, w.owner.id)
    self_named = await web.post(url, json={"new_owner_user_id": str(w.owner.id)})
    assert self_named.status_code == 403
    await _as(web, db, boot, w.owner.id)  # (each real request loads the caller afresh)
    done = await web.post(url, json={"new_owner_user_id": str(w.other_admin.id)})
    assert done.status_code == 200, done.text
    assert done.json()["mode"] == "replacement"
    await _as(web, db, boot, w.owner.id)
    listing = (await web.get("/api/v1/spaces/organization")).json()
    assert next(s for s in listing["items"] if s["id"] == str(w.space.id))["owner_unavailable"] is False


async def test_the_org_owner_gets_a_clear_refusal_when_creating_a_space(
    web, db_session, org_and_owner
):
    db, boot = db_session, org_and_owner
    await _as(web, db, boot, boot.user_id)
    resp = await web.post(
        "/api/v1/spaces", json={"name": "HR", "slug": "hr", "owner": "organization"}
    )
    assert resp.status_code == 403
    assert "cannot create spaces" in resp.json()["detail"]
