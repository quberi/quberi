"""Every sign-in path refuses a suspended member, and none of them hops to another account.

The person in these tests has two accounts on one identity: a company account (email
``work-...``, suspended in most tests) and a personal workspace (email ``home-...``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update

from quberi_api.api import dependencies
from quberi_api.api.errors import register_exception_handlers
from quberi_api.api.v1.auth import router as auth_module
from quberi_api.api.v1.auth.router import auth_router
from quberi_api.core.config import get_settings
from quberi_api.domain.auth.models import VerificationCode
from quberi_api.domain.identity.models import ExternalIdentity, Identity, Organization, User
from quberi_api.services import members as members_service
from quberi_api.services import onboarding, org_membership
from quberi_api.services.auth import feishu_login, google_login, phone_auth
from quberi_api.services.auth import verification as verification_service
from quberi_api.services.auth.account_status import (
    ACCOUNT_UNAVAILABLE_MESSAGE,
    AccountUnavailableError,
    best_login_user,
    login_usable,
)
from quberi_api.services.auth.session import mint_session

PHONE = "+8613800138000"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


async def _person(db, boot, *, company_status="suspended", phone=None):
    """A person with a company account and a personal workspace on one identity."""

    tag = uuid.uuid4().hex[:8]
    home = f"home-{tag}@example.com"
    work = f"work-{tag}@example.com"
    await db.commit()
    personal = await onboarding.bootstrap_organization(
        db,
        name=f"Home {tag}",
        slug=f"home-{tag}",
        owner_email=home,
        owner_display_name="Person",
        tier="personal",
    )
    # Same email as the identity so the invite reuses the identity; then give the company
    # account its own address, as a real work mailbox would be.
    view = await members_service.invite_member(
        db,
        organization_id=boot.organization_id,
        invited_by_user_id=boot.user_id,
        email=home,
        display_name="Person",
        role_code="member",
    )
    await db.execute(
        update(User).where(User.id == view.user.id).values(email=work, status=company_status)
    )
    if phone:
        await db.execute(
            update(Identity).where(Identity.id == personal.identity_id).values(primary_phone=phone)
        )
    await db.commit()
    return SimpleNamespace(
        identity_id=personal.identity_id,
        company_user_id=view.user.id,
        company_org_id=boot.organization_id,
        personal_user_id=personal.user_id,
        personal_org_id=personal.organization_id,
        work=work,
        home=home,
    )


async def _add_code(db, key, code, purpose):
    db.add(
        VerificationCode(
            email=key,
            code_hash=verification_service._hash_code(key, code),
            purpose=purpose,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
    )
    await db.commit()


async def _org_count(db):
    await db.commit()
    return (await db.execute(select(func.count()).select_from(Organization))).scalar_one()


async def _external_count(db):
    await db.commit()
    return (await db.execute(select(func.count()).select_from(ExternalIdentity))).scalar_one()


async def _status(db, user_id):
    await db.commit()
    return (await db.execute(select(User.status).where(User.id == user_id))).scalar_one()


# ---------------------------------------------------------------------------
# Email code
# ---------------------------------------------------------------------------


async def test_email_code_for_a_suspended_account_is_refused_without_creating_anything(
    db_session, org_and_owner
):
    p = await _person(db_session, org_and_owner)
    await _add_code(db_session, p.work, "123456", "login")
    before = await _org_count(db_session)
    with pytest.raises(AccountUnavailableError) as err:
        await verification_service.verify_code(db_session, email=p.work, code="123456")
    assert str(err.value) == ACCOUNT_UNAVAILABLE_MESSAGE
    assert await _org_count(db_session) == before


async def test_email_code_does_not_hop_to_the_same_identitys_other_account(
    db_session, org_and_owner
):
    """The mailbox of a suspended account must not open the person's personal workspace."""

    p = await _person(db_session, org_and_owner)
    await _add_code(db_session, p.work, "123456", "login")
    with pytest.raises(AccountUnavailableError):
        await verification_service.verify_code(db_session, email=p.work, code="123456")


async def test_email_code_for_the_active_personal_address_still_signs_in(db_session, org_and_owner):
    p = await _person(db_session, org_and_owner)
    await _add_code(db_session, p.home, "123456", "login")
    resolved = await verification_service.verify_code(db_session, email=p.home, code="123456")
    assert resolved.user_id == str(p.personal_user_id)


async def test_email_code_for_an_active_company_account_still_signs_in(db_session, org_and_owner):
    p = await _person(db_session, org_and_owner, company_status="active")
    await _add_code(db_session, p.work, "123456", "login")
    resolved = await verification_service.verify_code(db_session, email=p.work, code="123456")
    assert resolved.user_id == str(p.company_user_id)


# ---------------------------------------------------------------------------
# Phone
# ---------------------------------------------------------------------------


async def test_phone_signs_in_to_the_personal_workspace_when_the_company_account_is_suspended(
    db_session, org_and_owner
):
    p = await _person(db_session, org_and_owner, phone=PHONE)
    await _add_code(db_session, PHONE, "123456", phone_auth._PURPOSE)
    resolved = await phone_auth.verify_phone_and_resolve(db_session, phone=PHONE, code="123456")
    assert resolved.user_id == str(p.personal_user_id)
    assert resolved.organization_id == str(p.personal_org_id)


async def test_phone_of_a_fully_suspended_identity_is_refused_and_not_reprovisioned(
    db_session, org_and_owner
):
    p = await _person(db_session, org_and_owner, phone=PHONE)
    await db_session.execute(
        update(User).where(User.id == p.personal_user_id).values(status="suspended")
    )
    await db_session.commit()
    await _add_code(db_session, PHONE, "123456", phone_auth._PURPOSE)
    before = await _org_count(db_session)
    with pytest.raises(AccountUnavailableError):
        await phone_auth.verify_phone_and_resolve(db_session, phone=PHONE, code="123456")
    assert await _org_count(db_session) == before


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------


def _patch_google(monkeypatch, *, sub, email):
    async def _token(**_kw):
        return SimpleNamespace(access_token="t")

    async def _info(_token):
        return {"sub": sub, "email": email, "email_verified": True, "name": "Person"}

    monkeypatch.setattr(google_login, "_token_request", _token)
    monkeypatch.setattr(google_login, "_fetch_user_info", _info)


async def _google_login(db):
    return await google_login.complete_login(
        db,
        code="c",
        state=google_login._seal_state(google_login._PURPOSE_LOGIN),
        redirect_uri="http://test/cb",
    )


async def _link(db, identity_id, provider, subject):
    db.add(ExternalIdentity(identity_id=identity_id, provider=provider, subject=subject))
    await db.commit()


async def test_google_subject_signs_in_to_the_personal_workspace(
    db_session, org_and_owner, monkeypatch
):
    p = await _person(db_session, org_and_owner)
    sub = f"g-{uuid.uuid4().hex}"
    await _link(db_session, p.identity_id, "google", sub)
    _patch_google(monkeypatch, sub=sub, email=p.work)
    resolved = await _google_login(db_session)
    assert resolved.user_id == str(p.personal_user_id)


async def test_google_subject_of_a_fully_suspended_identity_is_refused(
    db_session, org_and_owner, monkeypatch
):
    p = await _person(db_session, org_and_owner)
    await db_session.execute(
        update(User).where(User.id == p.personal_user_id).values(status="suspended")
    )
    await db_session.commit()
    sub = f"g-{uuid.uuid4().hex}"
    await _link(db_session, p.identity_id, "google", sub)
    _patch_google(monkeypatch, sub=sub, email=p.home)
    before = await _org_count(db_session)
    with pytest.raises(AccountUnavailableError):
        await _google_login(db_session)
    assert await _org_count(db_session) == before


async def test_a_new_google_subject_with_a_suspended_email_is_refused_and_not_linked(
    db_session, org_and_owner, monkeypatch
):
    p = await _person(db_session, org_and_owner)
    _patch_google(monkeypatch, sub=f"g-{uuid.uuid4().hex}", email=p.work)
    links, orgs = await _external_count(db_session), await _org_count(db_session)
    with pytest.raises(AccountUnavailableError):
        await _google_login(db_session)
    assert await _external_count(db_session) == links
    assert await _org_count(db_session) == orgs


# ---------------------------------------------------------------------------
# Feishu
# ---------------------------------------------------------------------------


def _patch_feishu(monkeypatch, *, open_id, email=None):
    async def _token(**_kw):
        return SimpleNamespace(access_token="t")

    async def _info(_token):
        return {"open_id": open_id, "name": "Person", "email": email or ""}

    monkeypatch.setattr(feishu_login, "_token_request", _token)
    monkeypatch.setattr(feishu_login, "_fetch_user_info", _info)


async def _feishu_login(db):
    return await feishu_login.complete_login(
        db,
        code="c",
        state=feishu_login._seal_state(feishu_login._PURPOSE_LOGIN),
        redirect_uri="http://test/cb",
    )


def _ticket(open_id):
    return feishu_login._seal_bind_ticket(subject=open_id, name="Person", email=None)


async def test_feishu_open_id_of_a_fully_suspended_identity_is_refused(
    db_session, org_and_owner, monkeypatch
):
    p = await _person(db_session, org_and_owner)
    await db_session.execute(
        update(User).where(User.id == p.personal_user_id).values(status="suspended")
    )
    await db_session.commit()
    open_id = f"ou_{uuid.uuid4().hex}"
    await _link(db_session, p.identity_id, "lark", open_id)
    _patch_feishu(monkeypatch, open_id=open_id)
    with pytest.raises(AccountUnavailableError):
        await _feishu_login(db_session)


async def test_feishu_open_id_signs_in_to_the_personal_workspace(
    db_session, org_and_owner, monkeypatch
):
    p = await _person(db_session, org_and_owner)
    open_id = f"ou_{uuid.uuid4().hex}"
    await _link(db_session, p.identity_id, "lark", open_id)
    _patch_feishu(monkeypatch, open_id=open_id)
    outcome = await _feishu_login(db_session)
    assert outcome.resolved is not None
    assert outcome.resolved.user_id == str(p.personal_user_id)


async def test_feishu_bind_by_a_suspended_email_is_refused_and_not_linked(
    db_session, org_and_owner
):
    p = await _person(db_session, org_and_owner)
    await _add_code(db_session, p.work, "123456", feishu_login._PURPOSE_BIND)
    links, orgs = await _external_count(db_session), await _org_count(db_session)
    with pytest.raises(AccountUnavailableError):
        await feishu_login.bind_email_and_resolve(
            db_session, ticket=_ticket(f"ou_{uuid.uuid4().hex}"), email=p.work, code="123456"
        )
    assert await _external_count(db_session) == links
    assert await _org_count(db_session) == orgs


async def test_feishu_bind_by_phone_goes_to_the_personal_workspace(db_session, org_and_owner):
    p = await _person(db_session, org_and_owner, phone=PHONE)
    await _add_code(db_session, PHONE, "123456", feishu_login._PURPOSE_BIND)
    resolved = await feishu_login.bind_phone_and_resolve(
        db_session, ticket=_ticket(f"ou_{uuid.uuid4().hex}"), phone=PHONE, code="123456"
    )
    assert resolved.user_id == str(p.personal_user_id)


async def test_feishu_bind_by_the_phone_of_a_fully_suspended_identity_is_refused(
    db_session, org_and_owner
):
    p = await _person(db_session, org_and_owner, phone=PHONE)
    await db_session.execute(
        update(User).where(User.id == p.personal_user_id).values(status="suspended")
    )
    await db_session.commit()
    await _add_code(db_session, PHONE, "123456", feishu_login._PURPOSE_BIND)
    links, orgs = await _external_count(db_session), await _org_count(db_session)
    with pytest.raises(AccountUnavailableError):
        await feishu_login.bind_phone_and_resolve(
            db_session, ticket=_ticket(f"ou_{uuid.uuid4().hex}"), phone=PHONE, code="123456"
        )
    assert await _external_count(db_session) == links
    assert await _org_count(db_session) == orgs


# ---------------------------------------------------------------------------
# Placeholder adoption must not retire a suspended account
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", [feishu_login, google_login])
async def test_adopting_a_placeholder_account_never_archives_a_suspended_one(
    db_session, org_and_owner, module
):
    p = await _person(db_session, org_and_owner)
    provider = module._PROVIDER
    subject = f"s-{uuid.uuid4().hex}"
    await _link(db_session, p.identity_id, provider, subject)
    other = await onboarding.bootstrap_organization(
        db_session,
        name="Other",
        slug=f"other-{uuid.uuid4().hex[:6]}",
        owner_email=f"other-{uuid.uuid4().hex[:6]}@example.com",
        owner_display_name="Other",
        tier="personal",
    )
    async with db_session.begin():
        await module._attach_subject_to(
            db_session,
            subject=subject,
            identity_id=other.identity_id,
            name="Person",
            provisional={
                "orphan": False,
                "is_placeholder": True,
                "user_id": str(p.company_user_id),
            },
        )
    assert await _status(db_session, p.company_user_id) == "suspended"


# ---------------------------------------------------------------------------
# Organization switcher
# ---------------------------------------------------------------------------


async def test_the_organization_switcher_leaves_out_a_suspended_account(db_session, org_and_owner):
    p = await _person(db_session, org_and_owner)
    mine = await org_membership.list_my_organizations(db_session, identity_id=p.identity_id)
    assert [m.organization_id for m in mine] == [p.personal_org_id]
    with pytest.raises(org_membership.NotFoundError):
        await org_membership.resolve_switch_target(
            db_session, identity_id=p.identity_id, organization_id=p.company_org_id
        )
    # An active account is still listed and switchable.
    await db_session.execute(
        update(User).where(User.id == p.company_user_id).values(status="active")
    )
    await db_session.commit()
    mine = await org_membership.list_my_organizations(db_session, identity_id=p.identity_id)
    assert {m.organization_id for m in mine} == {p.personal_org_id, p.company_org_id}


async def test_best_login_user_skips_suspended_and_archived(db_session, org_and_owner):
    p = await _person(db_session, org_and_owner)
    row = await best_login_user(db_session, identity_id=p.identity_id)
    assert row is not None and row[0].id == p.personal_user_id
    await db_session.execute(
        update(User).where(User.id == p.personal_user_id).values(status="archived")
    )
    await db_session.commit()
    assert await best_login_user(db_session, identity_id=p.identity_id) is None
    assert login_usable() is not None


# ---------------------------------------------------------------------------
# HTTP: error shape, switch-organization, popup callbacks
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(db_session):
    app = FastAPI()
    app.state.settings = get_settings()
    app.include_router(auth_router, prefix="/api/v1")
    register_exception_handlers(app)

    async def _session():
        yield db_session

    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[dependencies.get_infrastructure] = lambda: SimpleNamespace()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_verify_over_http_answers_401_account_unavailable(client, db_session, org_and_owner):
    p = await _person(db_session, org_and_owner)
    await _add_code(db_session, p.work, "123456", "login")
    resp = await client.post("/api/v1/auth/verify", json={"email": p.work, "code": "123456"})
    assert resp.status_code == 401
    assert resp.json() == {"detail": ACCOUNT_UNAVAILABLE_MESSAGE, "code": "account_unavailable"}


async def test_phone_verify_over_http_answers_401_account_unavailable(
    client, db_session, org_and_owner
):
    p = await _person(db_session, org_and_owner, phone=PHONE)
    await db_session.execute(
        update(User).where(User.id == p.personal_user_id).values(status="suspended")
    )
    await db_session.commit()
    await _add_code(db_session, PHONE, "123456", phone_auth._PURPOSE)
    resp = await client.post("/api/v1/auth/phone/verify", json={"phone": PHONE, "code": "123456"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "account_unavailable"


async def test_switch_organization_cannot_enter_a_suspended_account(
    client, db_session, org_and_owner
):
    p = await _person(db_session, org_and_owner)
    await db_session.commit()
    token = await mint_session(
        db_session,
        identity_id=p.identity_id,
        user_id=p.personal_user_id,
        organization_id=p.personal_org_id,
        email=p.home,
    )
    resp = await client.post(
        "/api/v1/auth/switch-organization",
        json={"organization_id": str(p.company_org_id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_a_popup_callback_for_a_suspended_account_shows_the_unavailable_message(
    client, db_session, org_and_owner, monkeypatch
):
    p = await _person(db_session, org_and_owner)

    async def _complete(_session, **_kw):
        return verification_service.ResolvedLogin(
            identity_id=str(p.identity_id),
            user_id=str(p.company_user_id),
            organization_id=str(p.company_org_id),
            organization_slug="x",
            email=p.work,
            display_name="Person",
        )

    monkeypatch.setattr(google_login, "complete_login", _complete)
    monkeypatch.setattr(auth_module, "_google_login_redirect_uri", lambda: "http://test/cb")
    state = google_login._seal_state(google_login._PURPOSE_LOGIN)
    resp = await client.get("/api/v1/auth/google/callback", params={"code": "c", "state": state})
    assert resp.status_code == 403
    assert "token" not in resp.text
    assert "unexpected" not in resp.text
