# ruff: noqa: F811  (fixtures are imported by name)
"""A deactivated member's outside-tool access ends, in the same transaction.

``revoke_all_for_user`` is what the deactivation calls. These tests pin what it does to
the grants and the audit trail, and what each way of using a grant answers afterwards.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select, update

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.identity.models import User
from quberi_api.domain.oauth.models import OAuthGrant
from quberi_api.services.oauth import core
from tests.test_external_image_search import _path, _read, signing_key  # noqa: F401
from tests.test_external_save import (  # noqa: F401
    CLIENT,
    _auth,
    _org,
    _tokens,
    redis_client,
)
from tests.test_image_search import _member as _invite
from tests.test_image_search_api import blobs  # noqa: F401
from tests.test_personal_space_freeze import (  # noqa: F401
    IMAGES,
    SEARCH,
    _world,
    fake_models,
    http,
    qdrant,
)


async def _grants(db, user_id):
    await db.commit()
    return (
        (await db.execute(select(OAuthGrant).where(OAuthGrant.user_id == user_id)))
        .scalars()
        .all()
    )


async def _revoke(db, boot, user_id):
    await db.commit()
    async with db.begin():
        return await core.revoke_all_for_user(
            db,
            user_id=user_id,
            actor_user_id=boot.user_id,
            reason=core.REVOKE_MEMBER_DEACTIVATED,
        )


async def _suspend(db, user_id):
    await db.commit()
    await db.execute(update(User).where(User.id == user_id).values(status="suspended"))
    await db.commit()


async def test_every_live_grant_of_the_member_is_revoked_and_audited(db_session, redis_client):
    boot = await _org(db_session)
    member = await _invite(db_session, boot, "member")
    other = await _invite(db_session, boot, "member")
    mine = [
        await _tokens(db_session, redis_client, boot, scope="knowledge:read", user_id=member.id)
        for _ in range(2)
    ]
    theirs = await _tokens(db_session, redis_client, boot, scope="knowledge:read", user_id=other.id)
    # One grant was already revoked by the member: it must not be touched or audited again.
    first = (await _grants(db_session, member.id))[0]
    await db_session.commit()
    await core.revoke_grant_for_user(
        db_session, grant_id=first.id, identity_id=member.identity_id
    )
    await db_session.commit()
    assert len(mine) == 2

    assert await _revoke(db_session, boot, member.id) == 1

    states = {g.id: g for g in await _grants(db_session, member.id)}
    assert all(g.revoked_at is not None for g in states.values())
    assert states[first.id].revoke_reason == "revoked_by_user"
    assert sorted(g.revoke_reason for g in states.values()) == [
        "member_deactivated",
        "revoked_by_user",
    ]
    # Someone else's grant is left alone.
    assert [g.revoked_at for g in await _grants(db_session, other.id)] == [None]
    assert theirs.access_token

    events = (
        (
            await db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "external_access.revoked",
                    AuditEvent.payload["reason"].astext == "member_deactivated",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    event = events[0]
    assert event.actor_id == boot.user_id  # the administrator, not the member
    assert event.organization_id == boot.organization_id
    blob = json.dumps(event.payload) + json.dumps([str(m) for m in mine])
    for token in mine:
        assert token.access_token not in json.dumps(event.payload)
        assert token.refresh_token not in json.dumps(event.payload)
    assert "access_hash" not in blob and "refresh_hash" not in blob

    # A second call finds nothing to do and writes nothing.
    assert await _revoke(db_session, boot, member.id) == 0
    total = (
        await db_session.execute(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.action == "external_access.revoked",
                AuditEvent.payload["reason"].astext == "member_deactivated",
            )
        )
    ).scalar_one()
    assert total == 1


async def test_after_deactivation_the_access_token_and_the_refresh_token_stop_working(
    db_session, redis_client
):
    boot = await _org(db_session)
    member = await _invite(db_session, boot, "member")
    tokens = await _tokens(
        db_session, redis_client, boot, scope="knowledge:read", user_id=member.id
    )
    caller = await core.authenticate_access_token(db_session, tokens.access_token)
    assert caller.user.id == member.id
    await db_session.commit()

    await _suspend(db_session, member.id)
    await _revoke(db_session, boot, member.id)

    with pytest.raises(core.ExternalAuthError) as bad_access:
        await core.authenticate_access_token(db_session, tokens.access_token)
    assert bad_access.value.code == "invalid_token"
    await db_session.rollback()
    with pytest.raises(core.OAuthError) as bad_refresh:
        await core.refresh_tokens(
            db_session, refresh_token=tokens.refresh_token, client_id=CLIENT
        )
    assert bad_refresh.value.error == "invalid_grant"
    await db_session.rollback()


async def test_a_suspended_member_cannot_get_new_tokens_from_a_code(db_session, redis_client):
    """Signing in again through an outside tool is refused with the existing wording."""
    from tests.test_external_save import REDIRECT, _pkce

    boot = await _org(db_session)
    member = await _invite(db_session, boot, "member")
    verifier, challenge = _pkce()
    code = await core.create_authorization_code(
        redis_client,
        client_id=CLIENT,
        redirect_uri=REDIRECT,
        code_challenge=challenge,
        user_id=member.id,
        organization_id=boot.organization_id,
        scope="knowledge:read",
    )
    await _suspend(db_session, member.id)
    with pytest.raises(core.OAuthError) as refused:
        await core.exchange_code(
            db_session,
            redis_client,
            code=code,
            code_verifier=verifier,
            redirect_uri=REDIRECT,
            client_id=CLIENT,
            client_name="Harness",
        )
    assert refused.value.error == "invalid_grant"
    assert "no longer available" in refused.value.description
    await db_session.rollback()


async def test_an_old_preview_link_stops_working_when_the_member_is_deactivated(
    http, db_session, redis_client, qdrant, blobs
):
    w = await _world(db_session, qdrant, blobs)
    tokens = await _read(db_session, redis_client, w.boot, user_id=w.leaver.id)
    pics = (await http.post(IMAGES, headers=_auth(tokens), json={"query": "red"})).json()
    link = _path(pics["images"][0]["preview_url"])
    assert (await http.get(link)).status_code == 200
    await db_session.commit()

    await _suspend(db_session, w.leaver.id)
    await _revoke(db_session, w.boot, w.leaver.id)

    assert (await http.get(link)).status_code in (401, 403, 404)
    await db_session.commit()
    after = await http.post(SEARCH, headers=_auth(tokens), json={"query": "passage"})
    assert after.status_code == 401
