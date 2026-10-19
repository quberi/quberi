# ruff: noqa: F811  (fixtures are imported by name)
"""Deactivating and reactivating a member, end to end over HTTP with real sessions.

Only the organisation owner may do either. A deactivation is one transaction: it ends the
member's web sessions and outside-tool grants, removes their Feishu and private model
credentials, freezes their personal space and is audited; or it does nothing at all.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import func, select, update

from quberi_api.api import dependencies
from quberi_api.api.errors import register_exception_handlers
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.api.v1.organizations import members as members_router
from quberi_api.core.config import get_settings
from quberi_api.domain.ai.models import AiModelSelection, AiProviderBinding
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.authz.models import Role, RoleBinding
from quberi_api.domain.documents.models import DocumentSource
from quberi_api.domain.identity.models import User
from quberi_api.domain.integrations.models import Integration, IntegrationUserToken
from quberi_api.domain.spaces.models import Space
from quberi_api.services import member_lifecycle, space_freeze
from quberi_api.services.auth.account_status import AccountUnavailableError
from quberi_api.services.auth.session import mint_session
from quberi_api.services.security.crypto import seal
from tests.test_external_save import _org, _tokens, redis_client  # noqa: F401
from tests.test_image_search import _member as _invite
from tests.test_oauth_member_revocation import _grants


@pytest.fixture
async def client(db_session):
    app = FastAPI()
    app.state.settings = get_settings()
    app.include_router(members_router.router, prefix="/api/v1")

    @app.get("/api/v1/ping")
    async def ping(caller: CallerContext = Depends(require_caller)):
        return {"user": str(caller.user.id)}

    register_exception_handlers(app)

    async def _session():
        yield db_session

    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[dependencies.get_infrastructure] = lambda: SimpleNamespace()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _member(db, boot, role="member"):
    """A colleague as plain values: a rolled-back request expires ORM rows."""
    user = await _invite(db, boot, role)
    return SimpleNamespace(
        id=user.id,
        identity_id=user.identity_id,
        email=user.email,
        display_name=user.display_name,
    )


async def _login(db, boot, person):
    await db.commit()
    token = await mint_session(
        db,
        identity_id=person.identity_id,
        user_id=person.id,
        organization_id=boot.organization_id,
        email=person.email,
    )
    await db.commit()
    return {"Authorization": f"Bearer {token}"}


async def _owner_headers(db, boot):
    owner = (await db.execute(select(User).where(User.id == boot.user_id))).scalar_one()
    return await _login(
        db,
        boot,
        SimpleNamespace(id=owner.id, identity_id=owner.identity_id, email=owner.email),
    )


def _url(user_id, verb="deactivate"):
    return f"/api/v1/organizations/current/members/{user_id}/{verb}"


async def _row(db, user_id):
    """The user's current values, as plain data (a rolled-back request expires ORM rows)."""
    await db.commit()
    status, version, attributes = (
        await db.execute(
            select(User.status, User.session_version, User.attributes).where(User.id == user_id)
        )
    ).one()
    await db.commit()
    return SimpleNamespace(status=status, session_version=version, attributes=attributes)


async def _audit(db, action, resource_id=None):
    await db.commit()
    stmt = select(AuditEvent).where(AuditEvent.action == action)
    if resource_id is not None:
        stmt = stmt.where(AuditEvent.resource_id == resource_id)
    rows = (await db.execute(stmt)).scalars().all()
    await db.commit()
    return rows


async def _personal(db, user_id):
    await db.commit()
    space = (
        await db.execute(
            select(Space).where(Space.owner_id == user_id, Space.preset == "personal")
        )
    ).scalar_one()
    await db.refresh(space)
    await db.commit()
    return space


async def _private_key(db, identity_id):
    binding = AiProviderBinding(
        identity_id=identity_id,
        provider_kind="openai_compat",
        vendor="deepseek",
        display_name="mine",
        base_url="https://api.example.com/v1",
        api_key_encrypted=seal("sk-test-secret-value"),
        status="active",
        is_platform_default=False,
        extra_config={},
    )
    await db.commit()
    db.add(binding)
    await db.flush()
    await db.commit()
    return binding.id


async def _second_owner(db, boot):
    """Another person who also holds the owner role (cannot be invited, so bind directly)."""
    person = await _member(db, boot, "member")
    role = (await db.execute(select(Role).where(Role.code == "owner"))).scalars().first()
    db.add(
        RoleBinding(
            organization_id=boot.organization_id,
            role_id=role.id,
            subject_type="user",
            subject_id=person.id,
            resource_type="organization",
            resource_id=boot.organization_id,
            conditions={},
        )
    )
    await db.commit()
    return person


# ---------------------------------------------------------------------------
# Who may, and whom
# ---------------------------------------------------------------------------


async def test_only_the_organisation_owner_may_deactivate_or_reactivate(client, db_session):
    boot = await _org(db_session)
    admin = await _member(db_session, boot, "admin")
    plain = await _member(db_session, boot, "member")
    target = await _member(db_session, boot, "member")
    for who in (admin, plain):
        headers = await _login(db_session, boot, who)
        for verb in ("deactivate", "reactivate"):
            resp = await client.post(_url(target.id, verb), headers=headers)
            assert resp.status_code == 403, (who.display_name, verb)
    assert (await _row(db_session, target.id)).status == "active"
    assert await _audit(db_session, "member.deactivated") == []


async def test_an_owner_of_another_organisation_cannot_reach_the_member(client, db_session):
    boot = await _org(db_session)
    other = await _org(db_session)
    target = await _member(db_session, boot, "member")
    resp = await client.post(
        _url(target.id), headers=await _owner_headers(db_session, other)
    )
    assert resp.status_code == 404
    assert (await _row(db_session, target.id)).status == "active"


async def test_an_unknown_member_is_404_and_a_bad_id_is_422(client, db_session):
    boot = await _org(db_session)
    headers = await _owner_headers(db_session, boot)
    assert (await client.post(_url(uuid.uuid4()), headers=headers)).status_code == 404
    assert (await client.post(_url("not-a-uuid"), headers=headers)).status_code == 422


async def test_requests_without_a_session_are_401(client, db_session):
    boot = await _org(db_session)
    target = await _member(db_session, boot, "member")
    assert (await client.post(_url(target.id))).status_code == 401


async def test_nobody_can_deactivate_themselves_and_the_owner_cannot_be_deactivated(
    client, db_session
):
    boot = await _org(db_session)
    co_owner = await _second_owner(db_session, boot)
    headers = await _owner_headers(db_session, boot)

    resp = await client.post(_url(boot.user_id), headers=headers)
    assert resp.status_code == 400 and resp.json()["code"] == "cannot_deactivate_self"
    resp = await client.post(_url(co_owner.id), headers=headers)
    assert resp.status_code == 400 and resp.json()["code"] == "cannot_deactivate_owner"
    assert (await _row(db_session, boot.user_id)).status == "active"
    assert (await _row(db_session, co_owner.id)).status == "active"


async def test_an_archived_account_cannot_be_deactivated(client, db_session):
    boot = await _org(db_session)
    target = await _member(db_session, boot, "member")
    await db_session.execute(update(User).where(User.id == target.id).values(status="archived"))
    await db_session.commit()
    resp = await client.post(_url(target.id), headers=await _owner_headers(db_session, boot))
    assert resp.status_code == 409 and resp.json()["code"] == "member_archived"


# ---------------------------------------------------------------------------
# What a deactivation does
# ---------------------------------------------------------------------------


async def test_deactivating_ends_the_sessions_grants_and_personal_space_and_is_audited(
    client, db_session, redis_client
):
    boot = await _org(db_session)
    target = await _member(db_session, boot, "member")
    colleague = await _member(db_session, boot, "member")
    old_headers = await _login(db_session, boot, target)
    colleague_headers = await _login(db_session, boot, colleague)
    assert (await client.get("/api/v1/ping", headers=old_headers)).status_code == 200
    tokens = await _tokens(
        db_session, redis_client, boot, scope="knowledge:read", user_id=target.id
    )
    colleague_grant = await _tokens(
        db_session, redis_client, boot, scope="knowledge:read", user_id=colleague.id
    )
    await _private_key(db_session, target.identity_id)
    integration = Integration(
        organization_id=boot.organization_id, provider="feishu", display_name="Feishu"
    )
    db_session.add(integration)
    await db_session.flush()
    db_session.add(
        IntegrationUserToken(
            integration_id=integration.id,
            user_id=target.id,
            access_token_encrypted=seal("feishu-access"),
        )
    )
    db_session.add(
        DocumentSource(
            organization_id=boot.organization_id,
            kind="feishu",
            name="folder",
            config={"folder_token": "fldcnAAAAAAAAAAAAAAAA1", "target_space_id": str(uuid.uuid4())},
            created_by_user_id=target.id,
        )
    )
    await db_session.commit()
    before_version = (await _row(db_session, target.id)).session_version

    resp = await client.post(_url(target.id), headers=await _owner_headers(db_session, boot))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] is True
    assert body["member"]["id"] == str(target.id) and body["member"]["status"] == "suspended"
    assert body["effects"] == {
        "oauth_grants_revoked": 1,
        "feishu_tokens_removed": 1,
        "feishu_sources_paused": 1,
        "private_model_bindings_removed": 1,
        "org_model_selections_removed": 0,
        "other_org_selections_invalidated": 0,
        "personal_spaces_frozen": 1,
    }
    row = await _row(db_session, target.id)
    assert row.status == "suspended" and row.session_version == before_version + 1
    assert row.attributes["offboarding"]["previous_status"] == "active"
    # The old web session is refused with the code the web client signs out on.
    gone = await client.get("/api/v1/ping", headers=old_headers)
    assert gone.status_code == 401 and gone.json()["code"] == "session_invalid"
    # Somebody else's session and grant are untouched.
    assert (await client.get("/api/v1/ping", headers=colleague_headers)).status_code == 200
    assert [g.revoked_at is None for g in await _grants(db_session, colleague.id)] == [True]
    assert colleague_grant.access_token
    mine = await _grants(db_session, target.id)
    assert all(g.revoke_reason == "member_deactivated" for g in mine) and tokens.access_token
    # Signing in again is refused.
    with pytest.raises(AccountUnavailableError):
        await _login(db_session, boot, target)
    # Personal space frozen, not deleted.
    assert (await _personal(db_session, target.id)).status == "frozen"
    # The credentials are gone.
    assert (
        await db_session.execute(
            select(func.count()).select_from(AiProviderBinding).where(
                AiProviderBinding.identity_id == target.identity_id
            )
        )
    ).scalar_one() == 0
    assert (
        await db_session.execute(
            select(func.count()).select_from(IntegrationUserToken).where(
                IntegrationUserToken.user_id == target.id
            )
        )
    ).scalar_one() == 0
    source = (await db_session.execute(select(DocumentSource))).scalar_one()
    assert source.status == "paused"
    # One audit event, with counts and no secrets.
    (event,) = await _audit(db_session, "member.deactivated", target.id)
    assert event.actor_id == boot.user_id
    assert event.payload["previous_status"] == "active"
    assert event.payload["oauth_grants_revoked"] == 1
    assert len(event.payload["personal_space_ids"]) == 1
    text = json.dumps(event.payload)
    assert "sk-test-secret-value" not in text and tokens.access_token not in text
    assert len(await _audit(db_session, "space.frozen")) == 1


async def test_deactivating_twice_changes_nothing_the_second_time(client, db_session):
    boot = await _org(db_session)
    target = await _member(db_session, boot, "member")
    headers = await _owner_headers(db_session, boot)
    first = await client.post(_url(target.id), headers=headers)
    version = (await _row(db_session, target.id)).session_version
    second = await client.post(_url(target.id), headers=headers)

    assert first.json()["changed"] is True and second.status_code == 200
    assert second.json()["changed"] is False
    assert second.json()["member"]["status"] == "suspended"
    assert set(second.json()["effects"].values()) == {0}
    assert (await _row(db_session, target.id)).session_version == version
    assert len(await _audit(db_session, "member.deactivated", target.id)) == 1


async def test_the_response_member_is_the_row_the_list_shows(client, db_session):
    boot = await _org(db_session)
    target = await _member(db_session, boot, "admin")
    headers = await _owner_headers(db_session, boot)
    body = (await client.post(_url(target.id), headers=headers)).json()
    listed = (await client.get("/api/v1/organizations/current/members", headers=headers)).json()
    row = next(m for m in listed["items"] if m["id"] == str(target.id))
    assert body["member"] == row
    assert row["role_code"] == "admin" and row["status"] == "suspended"


async def test_an_invited_member_can_be_deactivated_and_comes_back_invited(client, db_session):
    boot = await _org(db_session)
    target = await _member(db_session, boot, "member")
    await db_session.execute(update(User).where(User.id == target.id).values(status="invited"))
    await db_session.commit()
    headers = await _owner_headers(db_session, boot)
    await client.post(_url(target.id), headers=headers)
    assert (await _row(db_session, target.id)).status == "suspended"
    resp = await client.post(_url(target.id, "reactivate"), headers=headers)
    assert resp.json()["member"]["status"] == "invited"


# ---------------------------------------------------------------------------
# Refusals and failures leave nothing behind
# ---------------------------------------------------------------------------


async def test_an_embedding_choice_on_the_private_key_refuses_with_no_effect(
    client, db_session, redis_client
):
    boot = await _org(db_session)
    target = await _member(db_session, boot, "member")
    key = await _private_key(db_session, target.identity_id)
    db_session.add(
        AiModelSelection(
            organization_id=boot.organization_id,
            purpose="embedding",
            provider_binding_id=key,
            model_id="text-embedding-3-small",
        )
    )
    await db_session.commit()
    await _tokens(db_session, redis_client, boot, scope="knowledge:read", user_id=target.id)
    before = await _row(db_session, target.id)
    version = before.session_version

    resp = await client.post(_url(target.id), headers=await _owner_headers(db_session, boot))

    assert resp.status_code == 409 and resp.json()["code"] == "org_embedding_uses_private_key"
    row = await _row(db_session, target.id)
    assert row.status == "active" and row.session_version == version
    assert [g.revoked_at for g in await _grants(db_session, target.id)] == [None]
    assert (await _personal(db_session, target.id)).status == "active"
    binding = await db_session.get(AiProviderBinding, key)
    assert binding.api_key_encrypted is not None
    assert await _audit(db_session, "member.deactivated") == []


async def test_a_failure_in_the_last_step_rolls_everything_back(
    client, db_session, redis_client, monkeypatch
):
    boot = await _org(db_session)
    target = await _member(db_session, boot, "member")
    await _private_key(db_session, target.identity_id)
    await _tokens(db_session, redis_client, boot, scope="knowledge:read", user_id=target.id)
    headers = await _owner_headers(db_session, boot)
    old = await _login(db_session, boot, target)
    version = (await _row(db_session, target.id)).session_version

    async def boom(*_a, **_k):
        raise RuntimeError("freeze failed")

    monkeypatch.setattr(space_freeze, "freeze_personal_spaces", boom)
    with pytest.raises(RuntimeError, match="freeze failed"):
        await client.post(_url(target.id), headers=headers)
    await db_session.rollback()

    row = await _row(db_session, target.id)
    assert row.status == "active" and row.session_version == version
    assert "offboarding" not in (row.attributes or {})
    assert [g.revoked_at for g in await _grants(db_session, target.id)] == [None]
    assert (
        await db_session.execute(
            select(func.count()).select_from(AiProviderBinding).where(
                AiProviderBinding.identity_id == target.identity_id
            )
        )
    ).scalar_one() == 1
    assert await _audit(db_session, "member.deactivated") == []
    assert (await client.get("/api/v1/ping", headers=old)).status_code == 200


# ---------------------------------------------------------------------------
# Reactivating
# ---------------------------------------------------------------------------


async def test_reactivating_restores_the_account_and_the_space_but_not_the_old_session_or_grants(
    client, db_session, redis_client
):
    boot = await _org(db_session)
    target = await _member(db_session, boot, "member")
    old = await _login(db_session, boot, target)
    await _tokens(db_session, redis_client, boot, scope="knowledge:read", user_id=target.id)
    headers = await _owner_headers(db_session, boot)
    await client.post(_url(target.id), headers=headers)

    resp = await client.post(_url(target.id, "reactivate"), headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] is True and body["personal_spaces_unfrozen"] == 1
    assert body["member"]["status"] == "active"
    row = await _row(db_session, target.id)
    assert row.status == "active" and "offboarding" not in (row.attributes or {})
    assert (await _personal(db_session, target.id)).status == "active"
    # The earlier session stays dead, a new sign-in works.
    assert (await client.get("/api/v1/ping", headers=old)).status_code == 401
    fresh = await _login(db_session, boot, target)
    assert (await client.get("/api/v1/ping", headers=fresh)).status_code == 200
    # The revoked grant is not brought back.
    assert [g.revoked_at is not None for g in await _grants(db_session, target.id)] == [True]
    (event,) = await _audit(db_session, "member.reactivated", target.id)
    assert event.payload["restored_status"] == "active"
    assert event.payload["personal_spaces_unfrozen"] == 1
    assert len(await _audit(db_session, "space.unfrozen")) == 1
    # And a second reactivation is a no-op.
    again = await client.post(_url(target.id, "reactivate"), headers=headers)
    assert again.json()["changed"] is False
    assert len(await _audit(db_session, "member.reactivated", target.id)) == 1


async def test_a_personal_space_the_owner_archived_comes_back_archived(client, db_session):
    boot = await _org(db_session)
    target = await _member(db_session, boot, "member")
    await db_session.execute(
        update(Space)
        .where(Space.owner_id == target.id, Space.preset == "personal")
        .values(status="archived")
    )
    await db_session.commit()
    headers = await _owner_headers(db_session, boot)
    await client.post(_url(target.id), headers=headers)
    assert (await _personal(db_session, target.id)).status == "frozen"
    await client.post(_url(target.id, "reactivate"), headers=headers)
    assert (await _personal(db_session, target.id)).status == "archived"


async def test_reactivating_an_active_member_does_nothing(client, db_session):
    boot = await _org(db_session)
    target = await _member(db_session, boot, "member")
    resp = await client.post(
        _url(target.id, "reactivate"), headers=await _owner_headers(db_session, boot)
    )
    assert resp.status_code == 200 and resp.json()["changed"] is False
    assert await _audit(db_session, "member.reactivated") == []


async def test_the_service_itself_refuses_a_caller_who_is_not_the_owner(db_session):
    from quberi_api.services.errors import ForbiddenError

    boot = await _org(db_session)
    admin = await _member(db_session, boot, "admin")
    target = await _member(db_session, boot, "member")
    await db_session.commit()
    with pytest.raises(ForbiddenError):
        await member_lifecycle.deactivate_member(
            db_session,
            organization_id=boot.organization_id,
            actor_user_id=admin.id,
            target_user_id=target.id,
        )
    await db_session.rollback()
    assert (await _row(db_session, target.id)).status == "active"
