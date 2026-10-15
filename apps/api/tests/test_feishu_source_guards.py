"""Who may connect a Feishu folder to a space, and what the shared app may read.

All organisations share one Feishu app and a folder token is not secret, so
these tests pin the rules that stop one organisation reaching another's
folders or documents:

* only a well-formed folder and a writable, in-organisation target space pass;
* one organisation per folder (first come), released with its last source;
* the person connecting must be able to open the folder with their own login;
* the shared-app endpoints are admin-only and limited to the caller's data.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from quberi_api.api.security import CallerContext
from quberi_api.api.v1.integrations import feishu as feishu_api
from quberi_api.api.v1.organizations import document_sources as sources_api
from quberi_api.domain.documents.models import Document, FeishuFolderClaim
from quberi_api.domain.identity.models import Organization
from quberi_api.domain.integrations.models import Integration
from quberi_api.services import document_sources as sources_service
from quberi_api.services import feishu_sources, onboarding
from quberi_api.services import members as members_service
from quberi_api.services import space_bindings as space_bindings_service
from quberi_api.services import spaces as spaces_service
from quberi_api.services.errors import (
    AlreadyExistsError,
    InvalidInputError,
    NotFoundError,
)
from quberi_api.services.integrations.feishu.client import FeishuError

TOKEN = "fldcnAAAAAAAAAAAAAAAA1"
TOKEN_2 = "fldcnBBBBBBBBBBBBBBBB2"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _space(db, org, name="Shared", slug=None):
    await db.commit()
    view = await spaces_service.create_collaborative_space(
        db,
        organization_id=org.organization_id,
        creator_user_id=org.user_id,
        name=name,
        slug=slug or f"s-{uuid.uuid4().hex[:8]}",
    )
    return view.space


async def _member(db, org, role_code="member"):
    await db.commit()
    view = await members_service.invite_member(
        db,
        organization_id=org.organization_id,
        invited_by_user_id=org.user_id,
        email=f"{role_code}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=role_code.title(),
        role_code=role_code,
    )
    return view.user


async def _grant(db, org, space, user, role):
    await db.commit()
    await space_bindings_service.add_binding(
        db,
        space_id=space.id,
        organization_id=org.organization_id,
        actor_user_id=org.user_id,
        subject_type="user",
        subject_id=user.id,
        role_code=role,
    )


async def _other_org(db):
    await db.commit()
    unique = uuid.uuid4().hex[:8]
    return await onboarding.bootstrap_organization(
        db,
        name=f"Other {unique}",
        slug=f"other-{unique}",
        owner_email=f"o-{unique}@example.com",
        owner_display_name="Other Owner",
        tier="team",
    )


async def _caller(db, org_boot, user):
    organization = await db.get(Organization, org_boot.organization_id)
    return CallerContext(user=user, organization=organization, is_platform_admin=False)


async def _owner_caller(db, org_boot):
    from quberi_api.domain.identity.models import User

    return await _caller(db, org_boot, await db.get(User, org_boot.user_id))


def _cfg(space, token=TOKEN):
    return {"folder_token": token, "target_space_id": str(space.id)}


async def _register(db, org, space, name, token=TOKEN):
    await db.commit()
    return await sources_service.register_source(
        db,
        organization_id=org.organization_id,
        actor_user_id=org.user_id,
        kind="feishu",
        name=name,
        config=_cfg(space, token),
        credentials=None,
        caching_policy="reference_only",
    )


async def _claims(db, token):
    from sqlalchemy import select

    return (
        (
            await db.execute(
                select(FeishuFolderClaim).where(FeishuFolderClaim.folder_token == token)
            )
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# prepare_config: shape of the config, target space, actor's right to write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "short",
        "has space in it 12345",
        "https://x.feishu.cn/drive/folder/abc12345",
        "a" * 129,
        None,
        123,
    ],
)
async def test_bad_folder_token_is_rejected(db_session, org_and_owner, bad):
    space = await _space(db_session, org_and_owner)
    with pytest.raises(InvalidInputError):
        await feishu_sources.prepare_config(
            db_session,
            organization_id=org_and_owner.organization_id,
            actor_user_id=org_and_owner.user_id,
            config={"folder_token": bad, "target_space_id": str(space.id)},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "", "not-a-uuid", 5])
async def test_bad_target_space_value_is_rejected(db_session, org_and_owner, bad):
    with pytest.raises(InvalidInputError):
        await feishu_sources.prepare_config(
            db_session,
            organization_id=org_and_owner.organization_id,
            actor_user_id=org_and_owner.user_id,
            config={"folder_token": TOKEN, "target_space_id": bad},
        )


@pytest.mark.asyncio
async def test_unknown_space_is_not_found(db_session, org_and_owner):
    with pytest.raises(NotFoundError):
        await feishu_sources.prepare_config(
            db_session,
            organization_id=org_and_owner.organization_id,
            actor_user_id=org_and_owner.user_id,
            config={"folder_token": TOKEN, "target_space_id": str(uuid.uuid4())},
        )


@pytest.mark.asyncio
async def test_space_of_another_organization_is_refused(db_session, org_and_owner):
    other = await _other_org(db_session)
    foreign = await _space(db_session, other, name="Theirs")
    with pytest.raises(NotFoundError):
        await feishu_sources.prepare_config(
            db_session,
            organization_id=org_and_owner.organization_id,
            actor_user_id=org_and_owner.user_id,
            config=_cfg(foreign),
        )


@pytest.mark.asyncio
async def test_viewer_cannot_target_a_space_but_editor_can(db_session, org_and_owner):
    space = await _space(db_session, org_and_owner)
    viewer = await _member(db_session, org_and_owner)
    await _grant(db_session, org_and_owner, space, viewer, "viewer")
    with pytest.raises(NotFoundError):
        await feishu_sources.prepare_config(
            db_session,
            organization_id=org_and_owner.organization_id,
            actor_user_id=viewer.id,
            config=_cfg(space),
        )
    await db_session.commit()  # close the read transaction the refusal left open
    editor = await _member(db_session, org_and_owner)
    await _grant(db_session, org_and_owner, space, editor, "editor")
    cfg, token = await feishu_sources.prepare_config(
        db_session,
        organization_id=org_and_owner.organization_id,
        actor_user_id=editor.id,
        config={**_cfg(space), "extra": "x", "organization_id": "evil"},
    )
    assert token == TOKEN
    # Only the two known keys survive.
    assert cfg == _cfg(space)


# ---------------------------------------------------------------------------
# Folder claims: one organisation per folder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_claims_the_folder_and_records_audit(db_session, org_and_owner):
    from sqlalchemy import select

    from quberi_api.domain.audit.models import AuditEvent

    space = await _space(db_session, org_and_owner)
    view = await _register(db_session, org_and_owner, space, "ops")
    claims = await _claims(db_session, TOKEN)
    assert [c.organization_id for c in claims] == [org_and_owner.organization_id]
    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.action == "document_source.registered",
                AuditEvent.resource_id == view.source.id,
            )
        )
    ).scalar_one()
    assert event.payload["folder_token"] == TOKEN
    assert event.payload["target_space_id"] == str(space.id)


@pytest.mark.asyncio
async def test_second_organization_cannot_connect_the_same_folder(
    db_session, org_and_owner
):
    space = await _space(db_session, org_and_owner)
    await _register(db_session, org_and_owner, space, "first")
    other = await _other_org(db_session)
    other_space = await _space(db_session, other, name="Theirs")
    with pytest.raises(AlreadyExistsError):
        await _register(db_session, other, other_space, "second")
    # The refused attempt left neither a source nor a changed claim.
    listed = await sources_service.list_sources(
        db_session, organization_id=other.organization_id
    )
    assert [v for v in listed if v.source.kind == "feishu"] == []
    claims = await _claims(db_session, TOKEN)
    assert [c.organization_id for c in claims] == [org_and_owner.organization_id]


@pytest.mark.asyncio
async def test_same_organization_can_connect_one_folder_to_two_spaces(
    db_session, org_and_owner
):
    a = await _space(db_session, org_and_owner, name="A")
    b = await _space(db_session, org_and_owner, name="B")
    await _register(db_session, org_and_owner, a, "to-a")
    await _register(db_session, org_and_owner, b, "to-b")
    assert len(await _claims(db_session, TOKEN)) == 1


@pytest.mark.asyncio
async def test_same_connection_name_gets_a_number_instead_of_failing(
    db_session, org_and_owner
):
    a = await _space(db_session, org_and_owner, name="A")
    b = await _space(db_session, org_and_owner, name="B")
    c = await _space(db_session, org_and_owner, name="C")
    names = []
    for space in (a, b, c):
        view = await _register(db_session, org_and_owner, space, "Feishu · test")
        names.append(view.source.name)
    assert names == ["Feishu · test", "Feishu · test (2)", "Feishu · test (3)"]


@pytest.mark.asyncio
async def test_claim_is_released_only_with_the_last_source(db_session, org_and_owner):
    a = await _space(db_session, org_and_owner, name="A")
    b = await _space(db_session, org_and_owner, name="B")
    first = await _register(db_session, org_and_owner, a, "to-a")
    second = await _register(db_session, org_and_owner, b, "to-b")
    await db_session.commit()
    await sources_service.delete_source(
        db_session,
        source_id=first.source.id,
        organization_id=org_and_owner.organization_id,
        actor_user_id=org_and_owner.user_id,
    )
    assert len(await _claims(db_session, TOKEN)) == 1  # still used by "to-b"
    await db_session.commit()
    await sources_service.delete_source(
        db_session,
        source_id=second.source.id,
        organization_id=org_and_owner.organization_id,
        actor_user_id=org_and_owner.user_id,
    )
    assert await _claims(db_session, TOKEN) == []
    # Now another organisation may take the folder.
    other = await _other_org(db_session)
    other_space = await _space(db_session, other, name="Theirs")
    await _register(db_session, other, other_space, "theirs")
    assert [c.organization_id for c in await _claims(db_session, TOKEN)] == [
        other.organization_id
    ]


@pytest.mark.asyncio
async def test_registering_with_an_unwritable_space_creates_no_claim(
    db_session, org_and_owner
):
    other = await _other_org(db_session)
    foreign = await _space(db_session, other, name="Theirs")
    with pytest.raises(NotFoundError):
        await _register(db_session, org_and_owner, foreign, "bad")
    assert await _claims(db_session, TOKEN) == []


# ---------------------------------------------------------------------------
# The person must be able to open the folder with their own Feishu login
# ---------------------------------------------------------------------------


async def _integration(db, org):
    row = Integration(
        organization_id=org.organization_id,
        provider="feishu",
        display_name="Feishu",
        status="active",
        config={},
    )
    db.add(row)
    await db.flush()
    await db.commit()
    return row


async def _verify(db, org, *, token_value, opener):
    async def get_token(_session, _integration_id, _user_id):
        return token_value

    await feishu_sources.verify_user_can_open_folder(
        db,
        organization_id=org.organization_id,
        user_id=org.user_id,
        folder_token=TOKEN,
        base_url="https://example.invalid/open-apis",
        get_user_token=get_token,
        open_folder=opener,
    )


@pytest.mark.asyncio
async def test_no_feishu_integration_means_auth_required(db_session, org_and_owner):
    async def opener(**_):  # pragma: no cover - must not be reached
        raise AssertionError("must not call Feishu without a user token")

    with pytest.raises(feishu_sources.FeishuAuthRequiredError):
        await _verify(db_session, org_and_owner, token_value="x", opener=opener)


@pytest.mark.asyncio
async def test_missing_user_token_means_auth_required(db_session, org_and_owner):
    await _integration(db_session, org_and_owner)

    async def opener(**_):  # pragma: no cover
        raise AssertionError("must not call Feishu without a user token")

    with pytest.raises(feishu_sources.FeishuAuthRequiredError):
        await _verify(db_session, org_and_owner, token_value=None, opener=opener)


@pytest.mark.asyncio
async def test_folder_the_person_cannot_open_is_refused(db_session, org_and_owner):
    await _integration(db_session, org_and_owner)

    async def opener(**_):
        raise FeishuError(1061004, "forbidden")

    with pytest.raises(feishu_sources.FeishuFolderAccessError):
        await _verify(db_session, org_and_owner, token_value="tok", opener=opener)


@pytest.mark.asyncio
async def test_folder_the_person_can_open_passes_and_uses_their_token(
    db_session, org_and_owner
):
    await _integration(db_session, org_and_owner)
    seen = {}

    async def opener(**kwargs):
        seen.update(kwargs)

    await _verify(db_session, org_and_owner, token_value="the-users-token", opener=opener)
    assert seen["user_access_token"] == "the-users-token"
    assert seen["folder_token"] == TOKEN


# ---------------------------------------------------------------------------
# REST handlers: admin-only, and limited to the caller's own organisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_app_endpoints_reject_ordinary_members(db_session, org_and_owner):
    member = await _member(db_session, org_and_owner)
    caller = await _caller(db_session, org_and_owner, member)
    for call in (
        feishu_api.feishu_ping(caller=caller, session=db_session),
        feishu_api.feishu_list_files(folder_token=TOKEN, caller=caller, session=db_session),
        feishu_api.feishu_document_content(
            document_id="doxcnXXXXXXXXXXXX", caller=caller, session=db_session
        ),
        sources_api.test_source(
            source_id=uuid.uuid4(), caller=caller, session=db_session
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_list_files_requires_a_folder_this_organization_connected(
    db_session, org_and_owner, monkeypatch
):
    async def fake_client():
        return object()

    async def fake_list(_client, folder_token=None, **_):
        return []

    monkeypatch.setattr(feishu_api, "get_platform_client", fake_client)
    monkeypatch.setattr(feishu_api, "list_files", fake_list)
    owner = await _owner_caller(db_session, org_and_owner)
    # No folder given: refused (would otherwise list the app's own root).
    with pytest.raises(HTTPException) as exc:
        await feishu_api.feishu_list_files(
            folder_token=None, caller=owner, session=db_session
        )
    assert exc.value.status_code == 404
    # A folder nobody connected: refused.
    with pytest.raises(HTTPException) as exc:
        await feishu_api.feishu_list_files(
            folder_token=TOKEN, caller=owner, session=db_session
        )
    assert exc.value.status_code == 404
    # A folder another organisation connected: refused, same answer.
    other = await _other_org(db_session)
    other_space = await _space(db_session, other, name="Theirs")
    await _register(db_session, other, other_space, "theirs")
    with pytest.raises(HTTPException) as exc:
        await feishu_api.feishu_list_files(
            folder_token=TOKEN, caller=owner, session=db_session
        )
    assert exc.value.status_code == 404
    # Our own connected folder: allowed.
    mine = await _space(db_session, org_and_owner)
    await _register(db_session, org_and_owner, mine, "mine", token=TOKEN_2)
    result = await feishu_api.feishu_list_files(
        folder_token=TOKEN_2, caller=owner, session=db_session
    )
    assert result.total == 0


@pytest.mark.asyncio
async def test_document_content_only_for_documents_synced_into_the_organization(
    db_session, org_and_owner, monkeypatch
):
    async def fake_client():
        return object()

    async def fake_text(_client, document_id):
        return f"text of {document_id}"

    monkeypatch.setattr(feishu_api, "get_platform_client", fake_client)
    monkeypatch.setattr(feishu_api, "get_document_text", fake_text)
    owner = await _owner_caller(db_session, org_and_owner)
    space = await _space(db_session, org_and_owner)
    local = await sources_service.ensure_local_source(
        db_session, organization_id=org_and_owner.organization_id
    ) if hasattr(sources_service, "ensure_local_source") else None
    if local is None:
        from quberi_api.services import document_sources as ds

        local = await ds.ensure_local_source(
            db_session, organization_id=org_and_owner.organization_id
        )
    await db_session.commit()

    def doc(org_id, space_id, token):
        return Document(
            organization_id=org_id,
            space_id=space_id,
            source_id=local.id,
            name=f"{token}.docx",
            mime_type="text/plain",
            size_bytes=1,
            checksum_sha256="0" * 64,
            external_ref={"doc_token": token},
        )

    db_session.add(doc(org_and_owner.organization_id, space.id, "doxcnMINE00000000"))
    other = await _other_org(db_session)
    other_space = await _space(db_session, other, name="Theirs")
    other_local = await sources_service.ensure_local_source(
        db_session, organization_id=other.organization_id
    )
    await db_session.commit()
    theirs = doc(other.organization_id, other_space.id, "doxcnTHEIRS000000")
    theirs.source_id = other_local.id
    db_session.add(theirs)
    await db_session.commit()

    ok = await feishu_api.feishu_document_content(
        document_id="doxcnMINE00000000", caller=owner, session=db_session
    )
    assert ok.content == "text of doxcnMINE00000000"
    for token in ("doxcnTHEIRS000000", "doxcnNEVERSYNCED00"):
        with pytest.raises(HTTPException) as exc:
            await feishu_api.feishu_document_content(
                document_id=token, caller=owner, session=db_session
            )
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_register_handler_maps_feishu_checks_to_http_errors(
    db_session, org_and_owner, monkeypatch
):
    owner = await _owner_caller(db_session, org_and_owner)
    space = await _space(db_session, org_and_owner)
    payload = sources_api.RegisterSourceRequest(
        kind="feishu", name="ops", config=_cfg(space), caching_policy="reference_only"
    )

    async def needs_auth(*_, **__):
        raise feishu_sources.FeishuAuthRequiredError("auth")

    async def denied(*_, **__):
        raise feishu_sources.FeishuFolderAccessError("denied")

    monkeypatch.setattr(feishu_sources, "verify_user_can_open_folder", needs_auth)
    with pytest.raises(HTTPException) as exc:
        await sources_api.register_source(payload, caller=owner, session=db_session)
    assert exc.value.status_code == 428

    monkeypatch.setattr(feishu_sources, "verify_user_can_open_folder", denied)
    with pytest.raises(HTTPException) as exc:
        await sources_api.register_source(payload, caller=owner, session=db_session)
    assert exc.value.status_code == 403
    # Neither refusal created a source or a claim.
    assert await _claims(db_session, TOKEN) == []

    async def allowed(*_, **__):
        return None

    monkeypatch.setattr(feishu_sources, "verify_user_can_open_folder", allowed)
    created = await sources_api.register_source(payload, caller=owner, session=db_session)
    assert created.kind == "feishu"
    assert len(await _claims(db_session, TOKEN)) == 1


@pytest.mark.asyncio
async def test_register_handler_still_requires_an_org_admin(
    db_session, org_and_owner, monkeypatch
):
    member = await _member(db_session, org_and_owner)
    space = await _space(db_session, org_and_owner)
    await _grant(db_session, org_and_owner, space, member, "editor")
    caller = await _caller(db_session, org_and_owner, member)

    async def allowed(*_, **__):  # pragma: no cover - must not be reached
        raise AssertionError("an ordinary member must be stopped before Feishu")

    monkeypatch.setattr(feishu_sources, "verify_user_can_open_folder", allowed)
    payload = sources_api.RegisterSourceRequest(
        kind="feishu", name="ops", config=_cfg(space), caching_policy="reference_only"
    )
    with pytest.raises(HTTPException) as exc:
        await sources_api.register_source(payload, caller=caller, session=db_session)
    assert exc.value.status_code == 403
