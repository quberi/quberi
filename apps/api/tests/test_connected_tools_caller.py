# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""Telling connections of one tool apart.

Three MCP connections all carry the registered name "MCP client". What tells them apart is
what each program says about itself when it connects (``clientInfo`` in ``initialize``). It is
stored with the connection and shown in the person's list of connected tools. It is a label
for people to read, never something the server trusts.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from quberi_api.api.security import CallerContext
from quberi_api.api.v1.me_external_access import list_grants
from quberi_api.domain.identity.models import Organization, User
from quberi_api.domain.oauth.models import OAuthGrant
from quberi_api.services.mcp.caller import LABEL_MAX, caller_label
from tests.test_mcp_handshake import _auth, _mcp_tokens, _org, http, redis_client  # noqa: F401


def _initialize(client_info=None, **extra):
    params = {"protocolVersion": "2025-06-18", **extra}
    if client_info is not None:
        params["clientInfo"] = client_info
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}


async def _label(db, tokens):
    await db.commit()
    value = (
        await db.execute(select(OAuthGrant.caller_label).where(OAuthGrant.id == tokens.grant_id))
    ).scalar_one()
    await db.commit()  # the next request starts its own transaction
    return value


# --------------------------------------------------------------------------
# What the label is made of
# --------------------------------------------------------------------------


def test_name_and_version_make_the_label():
    assert caller_label({"clientInfo": {"name": "codex-mcp-client", "version": "0.52.0"}}) == (
        "codex-mcp-client 0.52.0"
    )


def test_a_display_title_beats_the_technical_name():
    info = {"name": "claude-ai", "title": "Claude", "version": "1.0"}
    assert caller_label({"clientInfo": info}) == "Claude 1.0"


def test_a_name_without_a_version_is_fine():
    assert caller_label({"clientInfo": {"name": "codex"}}) == "codex"


def test_control_characters_and_length_are_cleaned_up():
    nasty = {"name": "evil\n\x00\x1b[31mname" + "x" * 500, "version": "9" * 500}
    label = caller_label({"clientInfo": nasty})
    assert label is not None
    assert "\n" not in label and "\x00" not in label and "\x1b" not in label
    assert len(label) <= LABEL_MAX


def test_the_user_agent_is_the_fallback_when_there_is_no_client_info():
    assert caller_label({}, "python-httpx/0.28.1") == "python-httpx/0.28.1"
    assert caller_label({"clientInfo": "not an object"}, "curl/8.0") == "curl/8.0"
    assert caller_label({"clientInfo": {"name": "   "}}, "curl/8.0") == "curl/8.0"


def test_client_info_wins_over_the_user_agent():
    assert caller_label({"clientInfo": {"name": "codex"}}, "node") == "codex"


def test_nothing_to_go_on_gives_none():
    assert caller_label({}) is None
    assert caller_label({}, "") is None
    assert caller_label({"clientInfo": {"name": 5}}, None) is None


# --------------------------------------------------------------------------
# Stored when the client connects
# --------------------------------------------------------------------------


async def test_a_connection_is_labelled_when_it_introduces_itself(http, db_session, redis_client):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot)
    assert await _label(db_session, tokens) is None

    resp = await http.post(
        "/", headers=_auth(tokens),
        json=_initialize({"name": "codex-mcp-client", "version": "0.52.0"}),
    )

    assert resp.status_code == 200
    assert await _label(db_session, tokens) == "codex-mcp-client 0.52.0"


async def test_two_connections_of_one_tool_get_their_own_labels(http, db_session, redis_client):
    boot = await _org(db_session)
    first = await _mcp_tokens(db_session, redis_client, boot)
    second = await _mcp_tokens(db_session, redis_client, boot)

    await http.post("/", headers=_auth(first), json=_initialize({"name": "codex", "version": "1"}))
    await http.post("/", headers=_auth(second), json=_initialize({"name": "Claude"}))

    assert await _label(db_session, first) == "codex 1"
    assert await _label(db_session, second) == "Claude"


async def test_a_new_version_replaces_the_old_label(http, db_session, redis_client):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot)

    await http.post("/", headers=_auth(tokens), json=_initialize({"name": "codex", "version": "1"}))
    await http.post("/", headers=_auth(tokens), json=_initialize({"name": "codex", "version": "2"}))

    assert await _label(db_session, tokens) == "codex 2"


async def test_the_user_agent_is_used_when_the_client_sent_no_info(http, db_session, redis_client):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot)

    await http.post(
        "/", headers={**_auth(tokens), "user-agent": "some-agent/3.1"}, json=_initialize()
    )

    assert await _label(db_session, tokens) == "some-agent/3.1"


async def test_nothing_is_stored_when_there_is_nothing_to_store(http, db_session, redis_client):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot)

    resp = await http.post(
        "/", headers={**_auth(tokens), "user-agent": ""}, json=_initialize()
    )

    assert resp.status_code == 200
    assert await _label(db_session, tokens) is None


async def test_a_disconnected_grant_is_left_alone(http, db_session, redis_client):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot)
    await http.post("/", headers=_auth(tokens), json=_initialize({"name": "codex"}))

    # Disconnect it behind the client's back, then let a stale session introduce itself again.
    from datetime import datetime, timezone

    grant = (await db_session.execute(select(OAuthGrant).where(OAuthGrant.id == tokens.grant_id))).scalar_one()
    grant.revoked_at = datetime.now(timezone.utc)
    await db_session.commit()
    from quberi_api.services.oauth import core

    await core.record_caller(db_session, grant_id=tokens.grant_id, label="something else")

    assert await _label(db_session, tokens) == "codex"


async def test_the_handshake_still_works_when_recording_fails(
    http, db_session, redis_client, monkeypatch
):
    from quberi_api.services.oauth import core

    async def boom(*_a, **_k):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(core, "record_caller", boom)
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot)

    resp = await http.post("/", headers=_auth(tokens), json=_initialize({"name": "codex"}))

    assert resp.status_code == 200
    assert resp.json()["result"]["serverInfo"]["name"] == "quberi"


# --------------------------------------------------------------------------
# Shown in the person's list
# --------------------------------------------------------------------------


async def test_the_connected_tools_list_carries_the_label(http, db_session, redis_client):
    boot = await _org(db_session)
    labelled = await _mcp_tokens(db_session, redis_client, boot)
    await _mcp_tokens(db_session, redis_client, boot)  # connected, never introduced itself
    await http.post("/", headers=_auth(labelled), json=_initialize({"name": "codex", "version": "7"}))
    await db_session.commit()
    user = await db_session.get(User, boot.user_id)
    org = await db_session.get(Organization, boot.organization_id)
    caller = CallerContext(user=user, organization=org, is_platform_admin=False)

    listed = await list_grants(caller=caller, session=db_session)

    by_id = {g.id: g for g in listed.items}
    assert len(by_id) == 2
    assert by_id[labelled.grant_id].caller_label == "codex 7"
    assert sorted(g.caller_label is None for g in listed.items) == [False, True]
    assert {g.client_name for g in listed.items} == {"MCP client"}
