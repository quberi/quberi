"""Write scope: the properties that must hold across the upgrade and over time.

``test_external_save.py`` covers the happy paths. These pin what makes adding a
write power *safe to ship*:

* nobody gains the ability to write by accident - not by naming no scope, not by
  refreshing a token, not by an older read-only grant living through the upgrade;
* write does not imply read (a tool asks for exactly what it needs);
* the save endpoint cannot be hammered;
* a revoked grant or an organisation that switched outside tools off can no
  longer write, at once.

Same stubbing as ``test_external_save.py``: MinIO and the index queue are faked,
everything else is real.
"""

# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from quberi_api.domain.oauth.models import OAuthGrant
from quberi_api.services.oauth import clients, core

# Reuse the exact fixtures and helpers of the sibling test module.
from tests.test_external_save import (  # noqa: F401, F811  (fixtures are used by name)
    CLIENT,
    _auth,
    _no_infra,
    _org,
    _tokens,
    http,
    redis_client,
)

SAVE = "/api/v1/external/save"
SEARCH = "/api/v1/external/search"
NOTE = {"title": "n", "text": "body"}


# ---------------------------------------------------------------------------
# Nobody gains write by accident
# ---------------------------------------------------------------------------


def test_naming_no_scope_grants_read_only_never_write():
    assert clients.normalise_scope(None) == "knowledge:read"
    assert clients.normalise_scope("") == "knowledge:read"
    assert clients.normalise_scope("   ") == "knowledge:read"
    assert "write" not in clients.DEFAULT_SCOPES[0]


@pytest.mark.parametrize(
    "asked",
    [
        "knowledge:write extra",       # one unknown word poisons the request
        "knowledge:WRITE",             # case matters
        "knowledge:writes",            # look-alike
        "knowledge:*",                 # no wildcards
        "write",
        "knowledge:read,knowledge:write",  # not space separated
    ],
)
def test_only_exact_known_scopes_are_accepted(asked):
    assert clients.normalise_scope(asked) is None


async def test_refreshing_a_read_only_grant_never_adds_write(
    db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    first = await _tokens(db_session, redis_client, boot, scope="knowledge:read")
    await db_session.commit()
    refreshed = await core.refresh_tokens(
        db_session, refresh_token=first.refresh_token, client_id=CLIENT
    )
    await db_session.commit()
    grant = (await db_session.scalars(select(OAuthGrant))).all()[-1]
    scope_now = grant.scope
    await db_session.commit()
    assert scope_now == "knowledge:read"
    assert refreshed.access_token != first.access_token


async def test_a_read_only_grant_still_cannot_save_after_refreshing(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    first = await _tokens(db_session, redis_client, boot, scope="knowledge:read")
    await db_session.commit()
    second = await core.refresh_tokens(
        db_session, refresh_token=first.refresh_token, client_id=CLIENT
    )
    await db_session.commit()
    resp = await http.post(SAVE, headers=_auth(second), json=NOTE)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "insufficient_scope"
    assert "blob" not in _no_infra  # nothing was stored


async def test_refreshing_a_write_grant_keeps_exactly_its_scope(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    first = await _tokens(
        db_session, redis_client, boot, scope="knowledge:read knowledge:write"
    )
    await db_session.commit()
    second = await core.refresh_tokens(
        db_session, refresh_token=first.refresh_token, client_id=CLIENT
    )
    await db_session.commit()
    grant = (await db_session.scalars(select(OAuthGrant))).all()[-1]
    scope_now = grant.scope
    await db_session.commit()  # close the read transaction before calling the API
    assert scope_now == "knowledge:read knowledge:write"
    assert (await http.post(SAVE, headers=_auth(second), json=NOTE)).status_code == 201


async def test_an_existing_pre_upgrade_grant_row_is_read_only(
    http, db_session, redis_client, _no_infra
):
    """A grant stored before write existed holds scope 'knowledge:read'."""
    boot = await _org(db_session)
    tokens = await _tokens(db_session, redis_client, boot, scope="knowledge:read")
    await db_session.commit()
    grant = (await db_session.scalars(select(OAuthGrant))).all()[-1]
    scope_now = grant.scope
    await db_session.commit()  # close the read transaction before calling the API
    assert scope_now == "knowledge:read"
    resp = await http.post(SAVE, headers=_auth(tokens), json=NOTE)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Write does not imply read
# ---------------------------------------------------------------------------


async def test_a_write_only_grant_cannot_search(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    tokens = await _tokens(db_session, redis_client, boot, scope="knowledge:write")
    await db_session.commit()
    resp = await http.post(SEARCH, headers=_auth(tokens), json={"query": "anything"})
    assert resp.status_code == 403


async def test_a_write_only_grant_can_still_save(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    tokens = await _tokens(db_session, redis_client, boot, scope="knowledge:write")
    await db_session.commit()
    resp = await http.post(SAVE, headers=_auth(tokens), json=NOTE)
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# It stops when it should stop
# ---------------------------------------------------------------------------


async def test_turning_the_organisation_switch_off_stops_saving_at_once(
    http, db_session, redis_client, _no_infra
):
    from quberi_api.domain.identity.models import Organization

    boot = await _org(db_session)
    tokens = await _tokens(
        db_session, redis_client, boot, scope="knowledge:read knowledge:write"
    )
    await db_session.commit()
    assert (await http.post(SAVE, headers=_auth(tokens), json=NOTE)).status_code == 201

    org = await db_session.get(Organization, boot.organization_id)
    org.allow_external_tools = False
    await db_session.commit()

    stored_before = _no_infra.get("blob")
    resp = await http.post(SAVE, headers=_auth(tokens), json={"title": "late", "text": "late"})
    assert resp.status_code in (401, 403)
    assert _no_infra.get("blob") == stored_before  # the second note never reached storage


async def test_a_revoked_grant_cannot_save(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    tokens = await _tokens(
        db_session, redis_client, boot, scope="knowledge:read knowledge:write"
    )
    await db_session.commit()
    grant = (await db_session.scalars(select(OAuthGrant))).all()[-1]
    # What revoking does to the row (see core.revoke_grant_for_user).
    grant.revoked_at = datetime.now(timezone.utc)
    grant.revoke_reason = "revoked_by_user"
    await db_session.commit()

    resp = await http.post(SAVE, headers=_auth(tokens), json=NOTE)
    assert resp.status_code == 401
    assert "blob" not in _no_infra


async def test_save_without_a_token_is_refused(http, _no_infra):
    resp = await http.post(SAVE, json=NOTE)
    assert resp.status_code == 401
    assert "blob" not in _no_infra


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


async def test_save_is_rate_limited_per_grant(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    tokens = await _tokens(
        db_session, redis_client, boot, scope="knowledge:read knowledge:write"
    )
    await db_session.commit()
    limit = 30  # _SAVE_RATE_LIMIT in api/v1/external.py
    for i in range(limit):
        resp = await http.post(SAVE, headers=_auth(tokens), json={"title": f"n{i}", "text": "t"})
        assert resp.status_code == 201, (i, resp.text)
    over = await http.post(SAVE, headers=_auth(tokens), json={"title": "over", "text": "t"})
    assert over.status_code == 429
    assert over.headers.get("retry-after") == "60"


async def test_the_save_limit_is_not_shared_between_grants(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    a = await _tokens(db_session, redis_client, boot, scope="knowledge:write")
    b = await _tokens(db_session, redis_client, boot, scope="knowledge:write")
    await db_session.commit()
    for i in range(30):
        resp = await http.post(SAVE, headers=_auth(a), json={"title": f"a{i}", "text": "t"})
        assert resp.status_code == 201
    assert (await http.post(SAVE, headers=_auth(a), json=NOTE)).status_code == 429
    assert (await http.post(SAVE, headers=_auth(b), json=NOTE)).status_code == 201


# ---------------------------------------------------------------------------
# Bad input never reaches storage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"title": "", "text": "x"},
        {"title": "x", "text": ""},
        {"title": "x" * 201, "text": "x"},
        {"title": "x", "text": "x", "extra": "field"},
        {"text": "no title"},
    ],
)
async def test_invalid_notes_are_rejected_before_storage(
    http, db_session, redis_client, _no_infra, body
):
    boot = await _org(db_session)
    tokens = await _tokens(db_session, redis_client, boot, scope="knowledge:write")
    await db_session.commit()
    resp = await http.post(SAVE, headers=_auth(tokens), json=body)
    assert resp.status_code == 422
    assert "blob" not in _no_infra
