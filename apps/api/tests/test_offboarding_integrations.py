"""What a deactivated member leaves behind in Feishu and in model settings.

* Their Feishu grants are deleted and the folders they connected stop syncing (the sync
  runs on the platform's own Feishu app, so deleting the personal token alone would not
  stop it), without retries and without touching what somebody else connected.
* Their private model keys are gone, and nothing that pointed at one answers with a 500.

Everything runs against the real test database; the Feishu network calls are stand-ins.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update

from quberi_api.api.errors import register_exception_handlers
from quberi_api.domain.ai.models import AiModelSelection, AiProviderBinding
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import DocumentSource
from quberi_api.domain.identity.models import User
from quberi_api.domain.integrations.models import (
    Integration,
    IntegrationOAuthState,
    IntegrationUserToken,
)
from quberi_api.services import document_sources as sources_service
from quberi_api.services import member_credentials
from quberi_api.services import members as members_service
from quberi_api.services.ai import resolver
from quberi_api.services.errors import OffboardingRequestError
from quberi_api.services.integrations.feishu import oauth as feishu_oauth
from quberi_api.services.rag import generate
from quberi_api.services.security.crypto import seal
from quberi_api.workers import feishu as feishu_worker
from tests.test_external_save import _org
from tests.test_image_search import _member as _invite

SECRET = "sk-test-secret-value"


def _db_session_factory(db):
    """What the worker calls to get a session: the test's own, left open."""

    class _Scope:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_exc):
            return False

    return lambda: _Scope()


async def _add(db, *rows):
    await db.commit()
    db.add_all(rows)
    await db.flush()
    await db.commit()


async def _feishu_source(
    db, boot, *, created_by, space_id, name=None, status="active", config=None
):
    source = DocumentSource(
        organization_id=boot.organization_id,
        kind="feishu",
        name=name or f"folder-{uuid.uuid4().hex[:6]}",
        config={
            "folder_token": "fldcnAAAAAAAAAAAAAAAA1",
            "target_space_id": str(space_id),
            **(config or {}),
        },
        status=status,
        created_by_user_id=created_by,
    )
    await _add(db, source)
    return source.id


async def _source(db, source_id):
    await db.commit()
    row = (
        await db.execute(select(DocumentSource).where(DocumentSource.id == source_id))
    ).scalar_one()
    await db.refresh(row)
    await db.commit()
    return row


async def _shared_space(db, boot):
    from tests.test_image_indexing import _space

    space = await _space(db, boot, "Handbook")
    await db.commit()
    return space


async def _clear(db, user_row, fn, actor):
    await db.commit()
    user = await db.get(User, user_row.id)
    async with db.begin():
        return await fn(db, user=user, actor_user_id=actor)


# ---------------------------------------------------------------------------
# Feishu
# ---------------------------------------------------------------------------


async def test_the_members_feishu_grants_go_and_only_the_folders_they_connected_stop(db_session):
    boot = await _org(db_session)
    leaver = await _invite(db_session, boot, "member")
    other = await _invite(db_session, boot, "member")
    space = await _shared_space(db_session, boot)
    integration = Integration(
        organization_id=boot.organization_id, provider="feishu", display_name="Feishu"
    )
    await _add(db_session, integration)
    await _add(
        db_session,
        IntegrationUserToken(
            integration_id=integration.id,
            user_id=leaver.id,
            access_token_encrypted=seal("access-one"),
            refresh_token_encrypted=seal("refresh-one"),
        ),
        IntegrationUserToken(
            integration_id=integration.id,
            user_id=other.id,
            access_token_encrypted=seal("access-two"),
        ),
        IntegrationOAuthState(
            state="half-done-flow",
            organization_id=boot.organization_id,
            user_id=leaver.id,
            provider="feishu",
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ),
    )
    mine = await _feishu_source(db_session, boot, created_by=leaver.id, space_id=space.id)
    already = await _feishu_source(
        db_session, boot, created_by=leaver.id, space_id=space.id, status="error"
    )
    theirs = await _feishu_source(db_session, boot, created_by=other.id, space_id=space.id)
    local = DocumentSource(
        organization_id=boot.organization_id,
        kind="local",
        name="local",
        config={},
        created_by_user_id=leaver.id,
    )
    await _add(db_session, local)

    removed, paused = await _clear(
        db_session, leaver, member_credentials.clear_feishu, boot.user_id
    )

    assert (removed, paused) == (1, 1)
    users_with_token = (
        (await db_session.execute(select(IntegrationUserToken.user_id))).scalars().all()
    )
    assert leaver.id not in users_with_token and other.id in users_with_token
    assert (
        await db_session.execute(
            select(func.count()).select_from(IntegrationOAuthState).where(
                IntegrationOAuthState.user_id == leaver.id
            )
        )
    ).scalar_one() == 0
    row = await _source(db_session, mine)
    assert row.status == "paused"
    assert row.config["paused_reason"] == "member_deactivated"
    assert row.config["paused_member_id"] == str(leaver.id)
    assert row.config["folder_token"]  # the rest of the configuration is kept
    assert (await _source(db_session, already)).status == "error"
    assert (await _source(db_session, theirs)).status == "active"
    assert (await db_session.get(DocumentSource, local.id)).status == "active"


class _Sync:
    """Stands in for the folder sync and counts how often it is asked to run."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, *_a, **_k):
        self.calls += 1
        return SimpleNamespace(indexed=0, skipped=0, failed=0, deleted=0)


@pytest.fixture
def sync(monkeypatch):
    from quberi_api.services.documents.connectors import feishu as connector

    counter = _Sync()
    monkeypatch.setattr(connector, "sync_folder_documents", counter)
    return counter


async def _run(db, source_id):
    await db.commit()
    ctx = {"session_factory": _db_session_factory(db), "qdrant": object()}
    out = await feishu_worker.sync_feishu_source(ctx, str(source_id))
    await db.commit()
    return out


async def test_a_paused_source_is_skipped_without_an_error_or_a_sync(db_session, sync):
    boot = await _org(db_session)
    space = await _shared_space(db_session, boot)
    source = await _feishu_source(
        db_session, boot, created_by=boot.user_id, space_id=space.id, status="paused"
    )
    out = await _run(db_session, source)
    assert out["skipped"] == "source not active"
    assert sync.calls == 0


async def test_a_source_whose_connector_was_deactivated_is_paused_and_skipped(db_session, sync):
    boot = await _org(db_session)
    leaver = await _invite(db_session, boot, "member")
    space = await _shared_space(db_session, boot)
    source = await _feishu_source(db_session, boot, created_by=leaver.id, space_id=space.id)
    await db_session.execute(update(User).where(User.id == leaver.id).values(status="suspended"))
    await db_session.commit()

    out = await _run(db_session, source)

    assert out["skipped"] == "connector suspended"
    assert sync.calls == 0
    row = await _source(db_session, source)
    assert row.status == "paused" and row.config["paused_reason"] == "member_deactivated"
    # Queued again, it now stops at the first check.
    assert (await _run(db_session, source))["skipped"] == "source not active"


@pytest.mark.parametrize("target", ["frozen", "missing"])
async def test_a_source_does_not_write_into_a_space_that_is_frozen_or_gone(
    db_session, sync, target
):
    from quberi_api.domain.spaces.models import Space

    boot = await _org(db_session)
    space = await _shared_space(db_session, boot)
    if target == "frozen":
        await db_session.execute(update(Space).where(Space.id == space.id).values(status="frozen"))
        await db_session.commit()
        space_id = space.id
    else:
        space_id = uuid.uuid4()
    source = await _feishu_source(db_session, boot, created_by=boot.user_id, space_id=space_id)

    out = await _run(db_session, source)

    assert out["skipped"] == "target space unavailable"
    assert sync.calls == 0
    assert (await _source(db_session, source)).status == "active"  # waits for the space


async def test_a_source_nobody_is_recorded_as_connecting_still_syncs(db_session, sync):
    boot = await _org(db_session)
    space = await _shared_space(db_session, boot)
    source = await _feishu_source(db_session, boot, created_by=None, space_id=space.id)
    out = await _run(db_session, source)
    assert out["source_id"] == str(source) and "indexed" in out
    assert sync.calls == 1


async def test_a_source_of_an_active_member_syncs(db_session, sync):
    boot = await _org(db_session)
    member = await _invite(db_session, boot, "member")
    space = await _shared_space(db_session, boot)
    source = await _feishu_source(db_session, boot, created_by=member.id, space_id=space.id)
    await _run(db_session, source)
    assert sync.calls == 1


async def test_the_manual_sync_says_why_only_when_it_is_the_deactivation(db_session):
    boot = await _org(db_session)
    space = await _shared_space(db_session, boot)
    by_member = await _feishu_source(
        db_session,
        boot,
        created_by=boot.user_id,
        space_id=space.id,
        status="paused",
        config={"paused_reason": "member_deactivated"},
    )
    plain = await _feishu_source(
        db_session, boot, created_by=boot.user_id, space_id=space.id, status="paused"
    )
    errored = await _feishu_source(
        db_session, boot, created_by=boot.user_id, space_id=space.id, status="error"
    )

    async def manual(source_id):
        await db_session.commit()
        return await sources_service.sync_source(
            db_session,
            source_id=source_id,
            organization_id=boot.organization_id,
            actor_user_id=boot.user_id,
        )

    said = await manual(by_member)
    assert not said.ok and "连接者已停用" in said.message and "deactivated" in said.message
    for other in (plain, errored):
        said = await manual(other)
        assert not said.ok
        assert "该来源当前不是启用状态" in said.message
        assert "deactivated" not in said.message


async def _manual_sync(db, boot, source_id):
    await db.commit()
    return await sources_service.sync_source(
        db,
        source_id=source_id,
        organization_id=boot.organization_id,
        actor_user_id=boot.user_id,
    )


@pytest.fixture
def manual_connector(monkeypatch):
    """Counts how often the manual sync reaches the connector (the part that writes)."""
    counter = _Sync()

    async def sync(config, credentials):
        await counter()

    monkeypatch.setattr(sources_service._EXTERNAL_CONNECTORS["feishu"], "sync", sync)
    return counter


async def test_the_manual_sync_refuses_a_frozen_target_and_works_again_after_it_thaws(
    db_session, manual_connector
):
    from quberi_api.domain.documents.models import Document
    from quberi_api.domain.spaces.models import Space

    boot = await _org(db_session)
    member = await _invite(db_session, boot, "member")
    space = await _shared_space(db_session, boot)
    # The source predates the freeze and its connector is still an active member.
    source = await _feishu_source(db_session, boot, created_by=member.id, space_id=space.id)

    async def documents():
        await db_session.commit()
        return (
            await db_session.execute(
                select(func.count()).select_from(Document).where(Document.space_id == space.id)
            )
        ).scalar_one()

    async def set_space(status):
        await db_session.execute(update(Space).where(Space.id == space.id).values(status=status))
        await db_session.commit()

    await set_space("frozen")
    said = await _manual_sync(db_session, boot, source)
    assert not said.ok and "目标空间当前不可用" in said.message
    assert manual_connector.calls == 0
    assert await documents() == 0
    audited = (
        await db_session.execute(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.action == "document_source.sync_attempted",
                AuditEvent.resource_id == source,
            )
        )
    ).scalar_one()
    assert audited == 0
    assert (await _source(db_session, source)).status == "active"  # waits for the space

    await set_space("active")
    said = await _manual_sync(db_session, boot, source)
    assert said.ok and manual_connector.calls == 1


async def test_the_manual_sync_refuses_a_suspended_connector_and_a_missing_target(
    db_session, manual_connector
):
    boot = await _org(db_session)
    leaver = await _invite(db_session, boot, "member")
    space = await _shared_space(db_session, boot)
    by_leaver = await _feishu_source(db_session, boot, created_by=leaver.id, space_id=space.id)
    gone = await _feishu_source(db_session, boot, created_by=boot.user_id, space_id=uuid.uuid4())
    await db_session.execute(update(User).where(User.id == leaver.id).values(status="suspended"))
    await db_session.commit()

    said = await _manual_sync(db_session, boot, by_leaver)
    assert not said.ok and "连接者已停用" in said.message
    said = await _manual_sync(db_session, boot, gone)
    assert not said.ok and "目标空间当前不可用" in said.message
    assert manual_connector.calls == 0


async def test_an_authorisation_started_before_deactivation_writes_no_new_grant(
    db_session, monkeypatch
):
    boot = await _org(db_session)
    leaver = await _invite(db_session, boot, "member")
    await _add(
        db_session,
        IntegrationOAuthState(
            state="started-before",
            organization_id=boot.organization_id,
            user_id=leaver.id,
            provider="feishu",
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ),
    )
    await db_session.execute(update(User).where(User.id == leaver.id).values(status="suspended"))
    await db_session.commit()

    async def never(**_kw):
        raise AssertionError("the code must not be exchanged for an unavailable account")

    monkeypatch.setattr(feishu_oauth, "_token_request", never)
    with pytest.raises(feishu_oauth.OAuthError, match="account unavailable"):
        await feishu_oauth.exchange_code(
            db_session, code="abc", state="started-before", redirect_uri="http://x/cb"
        )
    await db_session.rollback()
    assert (
        await db_session.execute(
            select(func.count()).select_from(IntegrationUserToken).where(
                IntegrationUserToken.user_id == leaver.id
            )
        )
    ).scalar_one() == 0


# ---------------------------------------------------------------------------
# Private model keys
# ---------------------------------------------------------------------------


async def _private_key(db, identity_id, name="my key"):
    binding = AiProviderBinding(
        identity_id=identity_id,
        provider_kind="openai_compat",
        vendor="deepseek",
        display_name=name,
        base_url="https://api.example.com/v1",
        api_key_encrypted=seal(SECRET),
        status="active",
        is_platform_default=False,
        extra_config={"models": ["my-chat"]},
    )
    await _add(db, binding)
    return binding.id


async def _select(db, org_id, purpose, binding_id, model_id="my-chat"):
    selection = AiModelSelection(
        organization_id=org_id, purpose=purpose, provider_binding_id=binding_id, model_id=model_id
    )
    await _add(db, selection)
    return selection.id


@pytest.fixture
async def platform_default(db_session, monkeypatch):
    """A platform chat model to fall back to."""
    binding = AiProviderBinding(
        provider_kind="openai_compat",
        vendor="deepseek",
        display_name="Platform default",
        base_url="https://platform.example.com/v1",
        api_key_encrypted=seal("platform-key"),
        status="active",
        is_platform_default=True,
        priority=-1000,
        extra_config={"models": ["platform-chat"]},
    )
    await _add(db_session, binding)
    monkeypatch.setenv("QUBERI_DEFAULT_CHAT_MODEL", "platform-chat")
    return binding.id


async def _bindings(db, identity_id):
    await db.commit()
    rows = (
        (
            await db.execute(
                select(AiProviderBinding).where(AiProviderBinding.identity_id == identity_id)
            )
        )
        .scalars()
        .all()
    )
    await db.commit()
    return rows


async def test_every_private_key_of_the_person_is_deleted_in_every_organisation(db_session):
    boot = await _org(db_session)
    boot2 = await _org(db_session)
    leaver = await _invite(db_session, boot, "member")
    # The same person has an account in a second organisation (the same global identity).
    await db_session.commit()
    again = await members_service.invite_member(
        db_session,
        organization_id=boot2.organization_id,
        invited_by_user_id=boot2.user_id,
        email=leaver.email,
        display_name="Same person",
        role_code="member",
    )
    assert again.user.identity_id == leaver.identity_id
    await _private_key(db_session, leaver.identity_id, "first")
    await _private_key(db_session, leaver.identity_id, "second")
    org_level = AiProviderBinding(
        organization_id=boot.organization_id,
        provider_kind="openai_compat",
        display_name="Shared key",
        api_key_encrypted=seal("org-key"),
        status="active",
        is_platform_default=False,
        created_by_user_id=leaver.id,
    )
    await _add(db_session, org_level)
    bystander = await _invite(db_session, boot, "member")
    others_key = await _private_key(db_session, bystander.identity_id)

    result = await _clear(
        db_session, leaver, member_credentials.clear_private_models, boot.user_id
    )

    assert (result.bindings_removed, result.org_selections_removed) == (2, 0)
    assert result.other_org_selections_invalidated == 0
    assert await _bindings(db_session, leaver.identity_id) == []
    assert await db_session.get(AiProviderBinding, org_level.id) is not None
    assert await db_session.get(AiProviderBinding, others_key) is not None
    # A person with no private keys is simply a zero.
    zero = await _clear(db_session, leaver, member_credentials.clear_private_models, boot.user_id)
    assert (zero.bindings_removed, zero.org_selections_removed) == (0, 0)


async def test_the_organisations_chat_choice_is_dropped_so_chat_uses_the_platform_default(
    db_session, platform_default
):
    boot = await _org(db_session)
    leaver = await _invite(db_session, boot, "member")
    key = await _private_key(db_session, leaver.identity_id)
    selection = await _select(db_session, boot.organization_id, "chat", key)
    await db_session.commit()
    before = await resolver.resolve_chat_provider(db_session, organization_id=boot.organization_id)
    assert before.binding_id == key
    await db_session.commit()

    result = await _clear(db_session, leaver, member_credentials.clear_private_models, boot.user_id)

    assert result.org_selections_removed == 1 and result.bindings_removed == 1
    assert await db_session.get(AiModelSelection, selection) is None
    await db_session.commit()
    now = await resolver.resolve_chat_provider(db_session, organization_id=boot.organization_id)
    assert now.binding_id == platform_default and now.is_platform_default
    await db_session.commit()


async def test_an_embedding_choice_on_the_private_key_stops_the_deactivation_with_no_effect(
    db_session,
):
    boot = await _org(db_session)
    leaver = await _invite(db_session, boot, "member")
    key = await _private_key(db_session, leaver.identity_id)
    chat = await _select(db_session, boot.organization_id, "chat", key)
    embedding = await _select(
        db_session, boot.organization_id, "embedding", key, "text-embedding-3-small"
    )

    with pytest.raises(OffboardingRequestError) as refused:
        await _clear(db_session, leaver, member_credentials.clear_private_models, boot.user_id)
    assert refused.value.code == "org_embedding_uses_private_key"
    assert refused.value.http_status == 409
    await db_session.rollback()

    # Nothing was changed, not even the chat choice that would have been dropped.
    row = await db_session.get(AiProviderBinding, key)
    assert row.api_key_encrypted and row.status == "active"
    assert await db_session.get(AiModelSelection, chat) is not None
    assert await db_session.get(AiModelSelection, embedding) is not None


@pytest.mark.parametrize("purpose", ["chat", "embedding"])
async def test_another_organisations_choice_is_kept_but_its_key_is_gone_and_it_is_told(
    db_session, purpose
):
    boot = await _org(db_session)
    other = await _org(db_session)
    leaver = await _invite(db_session, boot, "member")
    key = await _private_key(db_session, leaver.identity_id)
    model = "my-chat" if purpose == "chat" else "text-embedding-3-small"
    selection = await _select(db_session, other.organization_id, purpose, key, model)

    result = await _clear(db_session, leaver, member_credentials.clear_private_models, boot.user_id)

    assert result.other_org_selections_invalidated == 1 and result.org_selections_removed == 0
    assert result.bindings_removed == 1
    assert await db_session.get(AiModelSelection, selection) is not None
    row = await db_session.get(AiProviderBinding, key)
    await db_session.refresh(row)
    assert row.api_key_encrypted is None and row.status == "archived"
    await db_session.commit()
    events = (
        (
            await db_session.execute(
                select(AuditEvent).where(AuditEvent.action == "ai_model_selection.invalidated")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    event = events[0]
    assert event.organization_id == other.organization_id  # their log, not ours
    assert event.actor_type == "system"
    assert event.resource_id == selection
    assert event.payload == {"purpose": purpose, "reason": "private_binding_removed"}
    assert SECRET not in str(event.payload) and leaver.email not in str(event.payload)
    # The organisation now gets a plain "not configured" failure, not a quiet vendor switch.
    with pytest.raises(resolver.ProviderResolutionError):
        resolver_call = (
            resolver.resolve_chat_provider
            if purpose == "chat"
            else resolver.resolve_embedding_provider
        )
        await resolver_call(db_session, organization_id=other.organization_id)
    await db_session.rollback()


# ---------------------------------------------------------------------------
# Falling back, and the 503
# ---------------------------------------------------------------------------


async def test_a_conversation_pinned_to_a_removed_key_falls_back_and_the_preview_says_so(
    db_session, platform_default, caplog
):
    boot = await _org(db_session)
    leaver = await _invite(db_session, boot, "member")
    colleague = await _invite(db_session, boot, "member")
    key = await _private_key(db_session, leaver.identity_id)
    await db_session.commit()
    await _clear(db_session, leaver, member_credentials.clear_private_models, boot.user_id)

    with caplog.at_level("INFO"):
        described = await generate.describe_chat_model(
            db_session,
            organization_id=boot.organization_id,
            identity_id=colleague.identity_id,
            model_id="my-chat",
            binding_id=key,
        )
    assert described["model_id"] == "platform-chat"
    assert described["model_fallback"] is True
    assert str(key) in caplog.text
    assert SECRET not in caplog.text
    await db_session.commit()
    # A model that was never pinned carries no such mark.
    plain = await generate.describe_chat_model(
        db_session,
        organization_id=boot.organization_id,
        identity_id=colleague.identity_id,
        model_id=None,
        binding_id=None,
    )
    assert "model_fallback" not in plain


async def test_a_binding_that_cannot_chat_is_a_setup_error_and_is_not_swallowed(
    db_session, platform_default
):
    boot = await _org(db_session)
    member = await _invite(db_session, boot, "member")
    broken = AiProviderBinding(
        organization_id=boot.organization_id,
        provider_kind="local_embedding",
        display_name="Embedding only",
        status="active",
        is_platform_default=False,
    )
    await _add(db_session, broken)
    with pytest.raises(resolver.ProviderResolutionError) as raised:
        await generate.describe_chat_model(
            db_session,
            organization_id=boot.organization_id,
            identity_id=member.identity_id,
            model_id="bge-m3",
            binding_id=broken.id,
        )
    assert not isinstance(raised.value, resolver.BindingUnavailableError)
    await db_session.rollback()


async def test_an_unresolved_model_is_a_503_with_a_fixed_message():
    app = FastAPI()
    register_exception_handlers(app)
    org, binding = uuid.uuid4(), uuid.uuid4()

    @app.get("/boom")
    async def boom():
        raise resolver.ProviderResolutionError(f"org {org} selection references binding {binding}")

    @app.get("/gone")
    async def gone():
        raise resolver.BindingUnavailableError(f"binding {binding}")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for path in ("/boom", "/gone"):
            resp = await c.get(path)
            assert resp.status_code == 503, path
            assert resp.json()["code"] == "model_not_configured"
            assert str(org) not in resp.text and str(binding) not in resp.text


async def test_a_refusal_to_deactivate_carries_its_code_and_status():
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/refuse")
    async def refuse():
        raise OffboardingRequestError("org_embedding_uses_private_key", "message", 409)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/refuse")
    assert resp.status_code == 409
    assert resp.json() == {"detail": "message", "code": "org_embedding_uses_private_key"}
