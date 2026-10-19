"""Freeze and unfreeze a member's personal spaces.

Used only by the member deactivate / reactivate flow, never exposed as an endpoint:
a frozen space is unreadable to everyone, so there must be nobody who can "freeze" it
and thereby gain access. Both functions run inside the caller's open transaction and
neither commits.

Freezing sets ``status='frozen'`` and remembers the previous status under
``attributes['frozen']``, so a personal space the owner had archived on their own comes
back as ``archived`` and not ``active``. Rows and vectors are left in place.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.enums import FROZEN_SPACE_STATUS, READABLE_SPACE_STATUSES
from quberi_api.domain.spaces.models import Space

FREEZE_REASON = "member_deactivated"


def _personal_spaces_of(organization_id: uuid.UUID, owner_user_id: uuid.UUID):
    return select(Space).where(
        Space.organization_id == organization_id,
        Space.preset == "personal",
        Space.owner_type == "user",
        Space.owner_id == owner_user_id,
    )


async def freeze_personal_spaces(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> list[Space]:
    """Freeze every personal space owned by ``owner_user_id``; returns the ones changed."""

    spaces = (
        (
            await session.execute(
                _personal_spaces_of(organization_id, owner_user_id)
                .where(Space.status != FROZEN_SPACE_STATUS)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for space in spaces:
        from_status = space.status
        # Assign a new dict: in-place edits of a JSONB column are not tracked.
        space.attributes = {
            **(space.attributes or {}),
            "frozen": {
                "from_status": from_status,
                "reason": FREEZE_REASON,
                "member_id": str(owner_user_id),
                "at": datetime.now(timezone.utc).isoformat(),
                "by": str(actor_user_id),
            },
        }
        space.status = FROZEN_SPACE_STATUS
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="space.frozen",
                resource_type="space",
                resource_id=space.id,
                space_id=space.id,
                payload={
                    "slug": space.slug,
                    "preset": space.preset,
                    "from_status": from_status,
                    "reason": FREEZE_REASON,
                    "member_id": str(owner_user_id),
                },
            )
        )
    await session.flush()
    return list(spaces)


async def unfreeze_personal_spaces(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> list[Space]:
    """Return the member's frozen personal spaces to the status they had before."""

    spaces = (
        (
            await session.execute(
                _personal_spaces_of(organization_id, owner_user_id)
                .where(Space.status == FROZEN_SPACE_STATUS)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    restored: list[Space] = []
    for space in spaces:
        marker = (space.attributes or {}).get("frozen") or {}
        if marker.get("reason") != FREEZE_REASON:
            continue
        to_status = marker.get("from_status")
        if to_status not in READABLE_SPACE_STATUSES:
            to_status = "active"
        space.attributes = {k: v for k, v in (space.attributes or {}).items() if k != "frozen"}
        space.status = to_status
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="space.unfrozen",
                resource_type="space",
                resource_id=space.id,
                space_id=space.id,
                payload={
                    "to_status": to_status,
                    "reason": FREEZE_REASON,
                    "member_id": str(owner_user_id),
                },
            )
        )
        restored.append(space)
    await session.flush()
    return restored
