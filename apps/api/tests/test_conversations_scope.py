"""Regression tests for conversation renaming and knowledge scope.

Locks in behaviour verified manually earlier in this project:

* Renaming a conversation persists the new title; blank titles are rejected.
* Custom scope starts empty (an explicit source list, not "everything").
* Adding/removing a scope source updates the stored set immediately.
* An empty custom scope is a legitimate state (search nothing → the
  generation layer falls back to a model-only answer, tested separately in
  test_generate_scope.py) rather than an error.
"""

from __future__ import annotations

import pytest

from quberi_api.services import conversations as conversations_service
from quberi_api.services import spaces as spaces_service
from quberi_api.services.errors import InvalidInputError


async def _make_conversation(db_session, org_and_owner):
    view = await conversations_service.create_conversation(
        db_session,
        space_id=org_and_owner.personal_space_id,
        organization_id=org_and_owner.organization_id,
        user_id=org_and_owner.user_id,
        title="Original title",
    )
    return view.conversation


@pytest.mark.asyncio
async def test_rename_conversation_persists_title(db_session, org_and_owner):
    conv = await _make_conversation(db_session, org_and_owner)

    renamed = await conversations_service.rename_conversation(
        db_session,
        conversation_id=conv.id,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
        title="A much better title",
    )

    assert renamed.title == "A much better title"


@pytest.mark.asyncio
async def test_rename_conversation_rejects_blank_title(db_session, org_and_owner):
    conv = await _make_conversation(db_session, org_and_owner)

    with pytest.raises(InvalidInputError):
        await conversations_service.rename_conversation(
            db_session,
            conversation_id=conv.id,
            organization_id=org_and_owner.organization_id,
            caller_user_id=org_and_owner.user_id,
            title="   ",
        )


@pytest.mark.asyncio
async def test_conversation_defaults_to_auto_scope(db_session, org_and_owner):
    conv = await _make_conversation(db_session, org_and_owner)
    assert conv.scope_mode == "auto"


@pytest.mark.asyncio
async def test_entering_custom_scope_seeds_every_accessible_space(
    db_session, org_and_owner
):
    """Switching auto → custom seeds the explicit source list with every
    space the caller can currently reach, so the initial custom selection
    matches what auto was already searching — the user then unchecks
    whichever spaces (including the anchor) they don't want. This is a
    deliberate design choice, not an accident: an abrupt "custom starts
    empty" would silently break retrieval for anyone who just wanted to
    unpin the anchor space."""

    conv = await _make_conversation(db_session, org_and_owner)

    updated = await conversations_service.set_scope_mode(
        db_session,
        conversation_id=conv.id,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
        mode="custom",
    )
    assert updated.scope_mode == "custom"

    sources = await conversations_service.list_scope_sources(
        db_session,
        conversation_id=conv.id,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
    )
    # The only space this fresh org+owner can see is their personal space
    # (created by onboarding), so that's exactly what gets seeded.
    assert [s.space_id for s in sources] == [org_and_owner.personal_space_id]


@pytest.mark.asyncio
async def test_leaving_custom_scope_clears_sources(db_session, org_and_owner):
    conv = await _make_conversation(db_session, org_and_owner)
    await conversations_service.set_scope_mode(
        db_session,
        conversation_id=conv.id,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
        mode="custom",
    )
    await conversations_service.set_scope_mode(
        db_session,
        conversation_id=conv.id,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
        mode="auto",
    )
    sources = await conversations_service.list_scope_sources(
        db_session,
        conversation_id=conv.id,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
    )
    assert sources == []


@pytest.mark.asyncio
async def test_can_unpin_the_anchor_space_from_custom_scope(
    db_session, org_and_owner
):
    """The conversation's own (anchor) space is NOT force-included in
    custom scope — it must be removable like any other source. This was a
    deliberate behaviour change: earlier the anchor space was always
    searched regardless of the user's selection."""

    conv = await _make_conversation(db_session, org_and_owner)
    await conversations_service.set_scope_mode(
        db_session,
        conversation_id=conv.id,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
        mode="custom",
    )
    # Seeded with the anchor (personal) space already present.
    await conversations_service.remove_scope_source(
        db_session,
        conversation_id=conv.id,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
        space_id=conv.space_id,
    )
    sources_after = await conversations_service.list_scope_sources(
        db_session,
        conversation_id=conv.id,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
    )
    assert sources_after == []


@pytest.mark.asyncio
async def test_add_and_remove_scope_source(db_session, org_and_owner):
    conv = await _make_conversation(db_session, org_and_owner)
    await conversations_service.set_scope_mode(
        db_session,
        conversation_id=conv.id,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
        mode="custom",
    )
    # Start from a clean slate (unpin the seeded anchor) so this test is
    # only about the extra space, independent of the seeding behaviour
    # covered above.
    await conversations_service.remove_scope_source(
        db_session,
        conversation_id=conv.id,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
        space_id=conv.space_id,
    )

    extra_space = await spaces_service.create_collaborative_space(
        db_session,
        organization_id=org_and_owner.organization_id,
        creator_user_id=org_and_owner.user_id,
        name="Extra Knowledge",
        slug="extra-knowledge",
    )

    added = await conversations_service.add_scope_source(
        db_session,
        conversation_id=conv.id,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
        space_id=extra_space.space.id,
    )
    assert added.space_id == extra_space.space.id

    sources = await conversations_service.list_scope_sources(
        db_session,
        conversation_id=conv.id,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
    )
    assert [s.space_id for s in sources] == [extra_space.space.id]

    await conversations_service.remove_scope_source(
        db_session,
        conversation_id=conv.id,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
        space_id=extra_space.space.id,
    )
    sources_after = await conversations_service.list_scope_sources(
        db_session,
        conversation_id=conv.id,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
    )
    assert sources_after == []
