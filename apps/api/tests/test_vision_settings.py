"""Choosing the vision model, and the switch that lets pictures be described.

Two separate acts by an owner or admin: pick a model, then turn descriptions on. A different
model is a different company, so changing or clearing the pick turns the switch off again.
Real: database, services, routes, permission evaluator.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from quberi_api.api import dependencies
from quberi_api.api.errors import register_exception_handlers
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.api.v1.organizations.ai_providers import ai_providers_router
from quberi_api.api.v1.organizations.settings import organization_settings_router
from quberi_api.core.config import get_settings
from quberi_api.domain.ai.models import AiModelSelection, AiProviderBinding
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.identity.models import Organization, User
from quberi_api.services.ai import resolve_vision_provider
from quberi_api.services.security.crypto import seal
from tests.test_document_acl import _member

BASE = "/api/v1/organizations/current"


@pytest.fixture
async def web(db_session):
    app = FastAPI()
    app.state.settings = get_settings()
    register_exception_handlers(app)
    app.include_router(ai_providers_router, prefix="/api/v1")
    app.include_router(organization_settings_router, prefix="/api/v1")

    async def _session():
        yield db_session

    who = SimpleNamespace(user=None, organization=None)
    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[require_caller] = lambda: CallerContext(
        user=who.user, organization=who.organization, is_platform_admin=False
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.who = who  # type: ignore[attr-defined]
        yield client


async def _as(web, db, boot, user_id):
    user = await db.get(User, user_id)
    org = await db.get(Organization, boot.organization_id)
    await db.commit()
    web.who.user, web.who.organization = user, org


async def _binding(
    db, *, org_id=None, platform=False, identity_id=None, kind="openai_compat", status="active"
):
    row = AiProviderBinding(
        organization_id=org_id,
        identity_id=identity_id,
        provider_kind=kind,
        vendor="deepseek" if kind == "openai_compat" else None,
        display_name="test key",
        base_url="https://api.example.com/v1",
        api_key_encrypted=seal("sk-test-secret-value"),
        status=status,
        is_platform_default=platform,
        extra_config={"models": ["deepseek-flash", "deepseek-chat"]},
    )
    await db.commit()
    db.add(row)
    await db.flush()
    await db.commit()
    return row.id


async def _pick(web, binding, model="deepseek-flash"):
    return await web.put(
        f"{BASE}/ai-model-selections/vision",
        json={"model_id": model, "binding_id": str(binding)},
    )


@pytest.fixture
async def world(db_session, org_and_owner, web):
    boot = org_and_owner
    member = await _member(db_session, boot)
    await _as(web, db_session, boot, boot.user_id)
    binding = await _binding(db_session, org_id=boot.organization_id)
    return SimpleNamespace(boot=boot, member=member, binding=binding)


# -- choosing the model -----------------------------------------------------------------------


async def test_an_owner_picks_a_model_that_reads_pictures(web, world):
    assert (await _pick(web, world.binding)).status_code == 204

    chosen = (await web.get(f"{BASE}/ai-model-selections")).json()["vision"]
    assert chosen == {"model_id": "deepseek-flash", "binding_id": str(world.binding)}
    assert (await web.get(f"{BASE}/ai-model-selections")).json()["chat"]["model_id"] is None


async def test_with_no_pick_the_slot_is_empty(web, world):
    assert (await web.get(f"{BASE}/ai-model-selections")).json()["vision"] == {
        "model_id": None,
        "binding_id": None,
    }


async def test_a_listed_model_that_cannot_read_pictures_is_refused(web, world):
    refused = await _pick(web, world.binding, "deepseek-chat")
    assert refused.status_code == 400 and "does not read pictures" in refused.json()["detail"]


async def test_a_name_typed_for_their_own_endpoint_is_taken_on_the_administrators_word(web, world):
    assert (await _pick(web, world.binding, "my-company-vl-72b")).status_code == 204


async def test_only_an_openai_style_binding_can_carry_a_vision_model(db_session, web, world):
    local = await _binding(db_session, org_id=None, platform=True, kind="local_embedding")
    refused = await _pick(web, local, "my-vl")
    assert refused.status_code == 400 and "openai_compat" in refused.json()["detail"]


async def test_a_members_private_key_cannot_be_the_organisations_vision_model(
    db_session, web, world
):
    identity = (await db_session.get(User, world.member.id)).identity_id
    private = await _binding(db_session, identity_id=identity)
    refused = await _pick(web, private)
    assert refused.status_code == 400 and "private key" in refused.json()["detail"]


async def test_another_organisations_binding_cannot_be_picked(db_session, web, world):
    from tests.test_external_save import _org

    other = await _org(db_session)
    foreign = await _binding(db_session, org_id=other.organization_id)
    await _as(web, db_session, world.boot, world.boot.user_id)
    assert (await _pick(web, foreign)).status_code == 400


async def test_a_member_cannot_pick_or_clear_the_vision_model(db_session, web, world):
    await _pick(web, world.binding)
    await _as(web, db_session, world.boot, world.member.id)

    assert (await _pick(web, world.binding)).status_code == 403
    assert (await web.delete(f"{BASE}/ai-model-selections/vision")).status_code == 403
    chosen = (await web.get(f"{BASE}/ai-model-selections")).json()["vision"]
    assert chosen["model_id"] == "deepseek-flash"  # untouched


async def test_the_model_lists_say_which_models_read_pictures(web, world):
    catalog = {m["model_id"]: m for m in (await web.get(f"{BASE}/ai-models")).json()}
    assert catalog["deepseek-flash"]["supports_vision"] is True
    assert catalog["deepseek-chat"]["supports_vision"] is False


# -- the switch -------------------------------------------------------------------------------


async def test_descriptions_cannot_be_turned_on_without_a_model(db_session, web, world):
    refused = await web.patch(f"{BASE}/settings", json={"image_vision_enabled": True})
    assert refused.status_code == 400 and "choose a vision model" in refused.json()["detail"]
    # (A refused request rolls the test's one shared session back; a real request has its own.)
    await _as(web, db_session, world.boot, world.boot.user_id)
    assert (await web.get(f"{BASE}/settings")).json()["image_vision_enabled"] is False


async def test_descriptions_are_off_by_default_and_the_owner_turns_them_on_with_a_record(
    db_session, web, world
):
    assert (await web.get(f"{BASE}/settings")).json()["image_vision_enabled"] is False
    await _pick(web, world.binding)

    on = await web.patch(f"{BASE}/settings", json={"image_vision_enabled": True})

    assert on.status_code == 200 and on.json()["image_vision_enabled"] is True
    await db_session.commit()
    events = (
        (
            await db_session.execute(
                select(AuditEvent).where(AuditEvent.action == "organization.image_vision_changed")
            )
        )
        .scalars()
        .all()
    )
    assert [e.payload for e in events] == [{"enabled": True}]
    assert events[0].actor_id == world.boot.user_id


async def test_a_member_cannot_turn_descriptions_on(db_session, web, world):
    await _pick(web, world.binding)
    await _as(web, db_session, world.boot, world.member.id)
    refused = await web.patch(f"{BASE}/settings", json={"image_vision_enabled": True})
    assert refused.status_code == 403


async def test_other_settings_changes_leave_the_switch_alone(web, world):
    await _pick(web, world.binding)
    await web.patch(f"{BASE}/settings", json={"image_vision_enabled": True})

    other = await web.patch(f"{BASE}/settings", json={"allow_byok": False})

    assert other.json()["image_vision_enabled"] is True


async def _switch(web):
    return (await web.get(f"{BASE}/settings")).json()["image_vision_enabled"]


async def test_choosing_a_different_model_turns_the_switch_off_and_says_why(db_session, web, world):
    await _pick(web, world.binding)
    await web.patch(f"{BASE}/settings", json={"image_vision_enabled": True})

    await _pick(web, world.binding, "gpt-4o")

    assert await _switch(web) is False
    await db_session.commit()
    events = (
        (
            await db_session.execute(
                select(AuditEvent).where(AuditEvent.action == "organization.image_vision_changed")
            )
        )
        .scalars()
        .all()
    )
    off = [e for e in events if e.payload.get("enabled") is False]
    assert [e.payload for e in off] == [{"enabled": False, "reason": "vision model changed"}]
    assert off[0].actor_type == "system"


async def test_choosing_the_same_model_again_keeps_the_switch(web, world):
    await _pick(web, world.binding)
    await web.patch(f"{BASE}/settings", json={"image_vision_enabled": True})

    await _pick(web, world.binding)

    assert await _switch(web) is True


async def test_clearing_the_model_turns_the_switch_off(web, world):
    await _pick(web, world.binding)
    await web.patch(f"{BASE}/settings", json={"image_vision_enabled": True})

    assert (await web.delete(f"{BASE}/ai-model-selections/vision")).status_code == 204

    assert await _switch(web) is False


async def test_picking_a_model_for_the_first_time_does_not_turn_anything_on(web, world):
    await _pick(web, world.binding)
    assert await _switch(web) is False


# -- who can resolve it -----------------------------------------------------------------------


async def _select(db, boot, binding, model="deepseek-flash"):
    db.add(
        AiModelSelection(
            id=uuid.uuid4(),
            organization_id=boot.organization_id,
            purpose="vision",
            provider_binding_id=binding,
            model_id=model,
        )
    )
    await db.commit()


async def test_no_pick_means_no_provider_and_there_is_no_platform_default(
    db_session, org_and_owner, monkeypatch
):
    await _binding(db_session, platform=True)
    monkeypatch.setenv("QUBERI_DEFAULT_VISION_MODEL", "deepseek-flash")

    assert (
        await resolve_vision_provider(db_session, organization_id=org_and_owner.organization_id)
        is None
    )


async def test_a_picked_model_resolves_to_a_provider_with_its_spec(db_session, org_and_owner):
    binding = await _binding(db_session, org_id=org_and_owner.organization_id)
    await _select(db_session, org_and_owner, binding)

    resolved = await resolve_vision_provider(
        db_session, organization_id=org_and_owner.organization_id
    )

    assert resolved.model.model_id == "deepseek-flash" and resolved.model.supports_vision
    assert resolved.binding_id == binding


async def test_a_typed_model_name_resolves_and_is_treated_as_reading_pictures(
    db_session, org_and_owner
):
    binding = await _binding(db_session, org_id=org_and_owner.organization_id)
    await _select(db_session, org_and_owner, binding, "my-company-vl-72b")

    resolved = await resolve_vision_provider(
        db_session, organization_id=org_and_owner.organization_id
    )

    assert resolved.model.model_id == "my-company-vl-72b" and resolved.model.supports_vision


@pytest.mark.parametrize("flaw", ["inactive", "foreign", "private", "gone"])
async def test_a_pick_that_can_no_longer_be_used_resolves_to_nothing(
    db_session, org_and_owner, flaw
):
    from tests.test_external_save import _org

    boot = org_and_owner
    if flaw == "foreign":
        other = await _org(db_session)
        binding = await _binding(db_session, org_id=other.organization_id)
    else:
        member_identity = None
        if flaw == "private":
            member = await _member(db_session, boot)
            member_identity = (await db_session.get(User, member.id)).identity_id
        binding = await _binding(
            db_session,
            org_id=boot.organization_id if flaw != "private" else None,
            identity_id=member_identity,
            status="paused" if flaw == "inactive" else "active",
        )
    await _select(db_session, boot, binding)
    if flaw == "gone":
        await db_session.execute(
            AiProviderBinding.__table__.update()
            .where(AiProviderBinding.id == binding)
            .values(status="archived")
        )
        await db_session.commit()

    assert await resolve_vision_provider(db_session, organization_id=boot.organization_id) is None
