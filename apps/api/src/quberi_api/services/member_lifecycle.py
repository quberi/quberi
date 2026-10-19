"""Deactivate and reactivate an organisation member.

Deactivating is one transaction: the status change and every side effect (session
version, outside-tool grants, Feishu grants and syncs, private model keys, the personal
space freeze, the audit event) are committed together or not at all, so there is no
half-deactivated member. Reactivating brings back what was only switched off (the status
and the personal space) and nothing that was deleted.

Only the organisation owner may do either. The owner cannot be deactivated, and nobody
can deactivate themselves, so the organisation always has someone who can undo it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.identity.models import User
from quberi_api.services import member_credentials, space_freeze
from quberi_api.services.access_guard import is_org_owner
from quberi_api.services.errors import ForbiddenError, NotFoundError, OffboardingRequestError
from quberi_api.services.oauth import core as oauth_core

NOT_OWNER_MESSAGE = "only the organisation owner can deactivate or reactivate members"


@dataclass(frozen=True)
class DeactivationEffects:
    oauth_grants_revoked: int = 0
    feishu_tokens_removed: int = 0
    feishu_sources_paused: int = 0
    private_model_bindings_removed: int = 0
    org_model_selections_removed: int = 0
    other_org_selections_invalidated: int = 0
    personal_spaces_frozen: int = 0


@dataclass(frozen=True)
class DeactivationResult:
    changed: bool
    effects: DeactivationEffects = field(default_factory=DeactivationEffects)


@dataclass(frozen=True)
class ReactivationResult:
    changed: bool
    personal_spaces_unfrozen: int = 0


async def _locked_target(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
) -> User:
    if not await is_org_owner(session, organization_id=organization_id, user_id=actor_user_id):
        raise ForbiddenError(NOT_OWNER_MESSAGE)
    target = (
        await session.execute(
            select(User)
            .where(User.id == target_user_id, User.organization_id == organization_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if target is None:
        # Also the answer for a member of another organisation: their existence is not leaked.
        raise NotFoundError("member not found in this organisation")
    if target.status == "archived":
        raise OffboardingRequestError(
            "member_archived", "this member account is archived and cannot be managed", 409
        )
    return target


async def deactivate_member(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
) -> DeactivationResult:
    """Suspend a member and end everything that let them (or their keys) act."""

    async with session.begin():
        target = await _locked_target(
            session,
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
        )
        if target.id == actor_user_id:
            raise OffboardingRequestError(
                "cannot_deactivate_self", "you cannot deactivate yourself", 400
            )
        if await is_org_owner(session, organization_id=organization_id, user_id=target.id):
            raise OffboardingRequestError(
                "cannot_deactivate_owner", "the organisation owner cannot be deactivated", 400
            )
        if target.status == "suspended":
            return DeactivationResult(changed=False)

        # First, because it can refuse (an organisation embedding model on the member's
        # private key) and nothing else should be written before that is known.
        private = await member_credentials.clear_private_models(
            session, user=target, actor_user_id=actor_user_id
        )
        previous = target.status
        target.status = "suspended"
        target.session_version = (target.session_version or 0) + 1
        target.attributes = {
            **(target.attributes or {}),
            "offboarding": {"previous_status": previous, "by": str(actor_user_id)},
        }
        grants = await oauth_core.revoke_all_for_user(
            session,
            user_id=target.id,
            actor_user_id=actor_user_id,
            reason=oauth_core.REVOKE_MEMBER_DEACTIVATED,
        )
        tokens, sources = await member_credentials.clear_feishu(
            session, user=target, actor_user_id=actor_user_id
        )
        frozen = await space_freeze.freeze_personal_spaces(
            session,
            organization_id=organization_id,
            owner_user_id=target.id,
            actor_user_id=actor_user_id,
        )
        effects = DeactivationEffects(
            oauth_grants_revoked=grants,
            feishu_tokens_removed=tokens,
            feishu_sources_paused=sources,
            private_model_bindings_removed=private.bindings_removed,
            org_model_selections_removed=private.org_selections_removed,
            other_org_selections_invalidated=private.other_org_selections_invalidated,
            personal_spaces_frozen=len(frozen),
        )
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="member.deactivated",
                resource_type="user",
                resource_id=target.id,
                payload={
                    "email": target.email,
                    "display_name": target.display_name,
                    "previous_status": previous,
                    "oauth_grants_revoked": effects.oauth_grants_revoked,
                    "feishu_tokens_removed": effects.feishu_tokens_removed,
                    "feishu_sources_paused": effects.feishu_sources_paused,
                    "private_model_bindings_removed": effects.private_model_bindings_removed,
                    "org_model_selections_removed": effects.org_model_selections_removed,
                    "other_org_selections_invalidated": effects.other_org_selections_invalidated,
                    "personal_space_ids": [str(s.id) for s in frozen],
                },
            )
        )
        await session.flush()
    return DeactivationResult(changed=True, effects=effects)


async def reactivate_member(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
) -> ReactivationResult:
    """Let a suspended member back in.

    The status goes back to what it was and the personal space is unfrozen. Earlier web
    sessions stay ended, and deleted or revoked credentials are not recreated: the member
    signs in again and reconnects what they need.
    """

    async with session.begin():
        target = await _locked_target(
            session,
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
        )
        if target.status != "suspended":
            return ReactivationResult(changed=False)
        marker = (target.attributes or {}).get("offboarding") or {}
        restored = marker.get("previous_status")
        if restored not in ("active", "invited"):
            restored = "active"
        target.status = restored
        target.attributes = {k: v for k, v in (target.attributes or {}).items() if k != "offboarding"}
        thawed = await space_freeze.unfreeze_personal_spaces(
            session,
            organization_id=organization_id,
            owner_user_id=target.id,
            actor_user_id=actor_user_id,
        )
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="member.reactivated",
                resource_type="user",
                resource_id=target.id,
                payload={
                    "email": target.email,
                    "display_name": target.display_name,
                    "restored_status": restored,
                    "personal_spaces_unfrozen": len(thawed),
                },
            )
        )
        await session.flush()
    return ReactivationResult(changed=True, personal_spaces_unfrozen=len(thawed))
