"""Regression tests for conversation ↔ agent binding.

These lock in the behaviour that was manually verified (via throwaway
scripts, since deleted) earlier in this project's development:

* Binding an agent to a conversation stores it in ``attributes.agent`` and
  is readable back via ``_agent_binding``.
* A restricted agent the caller has no grant on cannot be bound.
* Clearing the binding removes it from ``attributes``.
* Binding a nonexistent agent raises ``NotFoundError``.
"""

from __future__ import annotations

import uuid

import pytest

from quberi_api.services import conversations as conversations_service
from quberi_api.services.agents import config as agents_config
from quberi_api.services.errors import InvalidInputError, NotFoundError


async def _make_conversation(db_session, org_and_owner):
    view = await conversations_service.create_conversation(
        db_session,
        space_id=org_and_owner.personal_space_id,
        organization_id=org_and_owner.organization_id,
        user_id=org_and_owner.user_id,
        title="Test conversation",
    )
    return view.conversation


async def _make_builtin_agent(db_session, org_and_owner, *, visibility="organization"):
    return await agents_config.create_agent(
        db_session,
        organization_id=org_and_owner.organization_id,
        actor_user_id=org_and_owner.user_id,
        name="Test Agent",
        description=None,
        icon="bot",
        system_prompt="You are a helpful assistant.",
        enabled_tools=["search_knowledge"],
        visibility=visibility,
    )


@pytest.mark.asyncio
async def test_bind_agent_to_conversation(db_session, org_and_owner):
    conv = await _make_conversation(db_session, org_and_owner)
    agent = await _make_builtin_agent(db_session, org_and_owner)

    updated = await conversations_service.set_conversation_agent(
        db_session,
        conversation_id=conv.id,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
        agent_id=agent.id,
    )

    assert updated.attributes.get("agent") == {"agent_id": str(agent.id)}
    assert conversations_service._agent_binding(updated) == agent.id


@pytest.mark.asyncio
async def test_clear_agent_binding(db_session, org_and_owner):
    conv = await _make_conversation(db_session, org_and_owner)
    agent = await _make_builtin_agent(db_session, org_and_owner)

    await conversations_service.set_conversation_agent(
        db_session,
        conversation_id=conv.id,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
        agent_id=agent.id,
    )
    cleared = await conversations_service.set_conversation_agent(
        db_session,
        conversation_id=conv.id,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
        agent_id=None,
    )

    assert "agent" not in (cleared.attributes or {})
    assert conversations_service._agent_binding(cleared) is None


@pytest.mark.asyncio
async def test_bind_nonexistent_agent_raises_not_found(db_session, org_and_owner):
    conv = await _make_conversation(db_session, org_and_owner)

    with pytest.raises(NotFoundError):
        await conversations_service.set_conversation_agent(
            db_session,
            conversation_id=conv.id,
            organization_id=org_and_owner.organization_id,
            caller_user_id=org_and_owner.user_id,
            agent_id=uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_bind_restricted_agent_without_grant_is_rejected(
    db_session, org_and_owner
):
    """A restricted agent is invisible/unusable to a member with no grant —
    binding it must fail with the same "not available to you" message the
    router surfaces as a 400, not silently succeed.

    Uses an org-visible collaborative space (not the owner's personal
    space) so the ordinary member can reach the conversation at all —
    otherwise the space-visibility check would reject the call first and
    we'd never exercise the agent-visibility check this test targets.
    """

    from quberi_api.services import members as members_service
    from quberi_api.services import space_bindings as space_bindings_service
    from quberi_api.services import spaces as spaces_service

    member = await members_service.invite_member(
        db_session,
        organization_id=org_and_owner.organization_id,
        invited_by_user_id=org_and_owner.user_id,
        email="member@example.com",
        display_name="Ordinary Member",
        role_code="member",
    )

    shared_space = await spaces_service.create_collaborative_space(
        db_session,
        organization_id=org_and_owner.organization_id,
        creator_user_id=org_and_owner.user_id,
        name="Shared Space",
        slug="shared-space",
    )
    # Space visibility is driven entirely by RoleBinding rows, not the
    # ``visibility`` column — grant the member explicit viewer access so
    # they can reach the conversation, isolating this test to the
    # agent-visibility check it's actually about.
    await space_bindings_service.add_binding(
        db_session,
        space_id=shared_space.space.id,
        organization_id=org_and_owner.organization_id,
        actor_user_id=org_and_owner.user_id,
        subject_type="user",
        subject_id=member.user.id,
        role_code="viewer",
    )
    conv = await conversations_service.create_conversation(
        db_session,
        space_id=shared_space.space.id,
        organization_id=org_and_owner.organization_id,
        user_id=org_and_owner.user_id,
    )

    agent = await _make_builtin_agent(
        db_session, org_and_owner, visibility="restricted"
    )

    with pytest.raises(InvalidInputError):
        await conversations_service.set_conversation_agent(
            db_session,
            conversation_id=conv.conversation.id,
            organization_id=org_and_owner.organization_id,
            caller_user_id=member.user.id,
            agent_id=agent.id,
        )
