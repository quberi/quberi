"""Who owns a space, and handing it to someone else.

A space is owned by whoever holds the ``owner`` role on it. The owner decides who else can
read it, including the organisation's administrators (see ``services.access_guard``), so a
space must never be left without one.

Two ways to change the owner:

* **Handover.** An active owner names a new owner. They keep editing access afterwards.
* **Replacement.** When no owner is available any more (they left, were suspended, or only
  hold the role by a leftover grant that no longer counts), the organisation owner, the
  super administrator, names a new one and the unavailable owners lose the role.

The new owner must be an active member of the organisation and cannot be the organisation
owner. The super administrator cannot name themselves: nobody gives themselves access.
Every change is audited with who handed the space to whom.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.authz.models import Role, RoleBinding
from quberi_api.domain.identity.models import User
from quberi_api.domain.spaces.models import Space
from quberi_api.services import access_guard
from quberi_api.services.errors import ForbiddenError, InvalidInputError, NotFoundError


@dataclass(frozen=True)
class OwnerTransfer:
    space_id: uuid.UUID
    new_owner_user_id: uuid.UUID
    mode: str  # "handover" | "replacement"
    removed_owner_user_ids: list[uuid.UUID]


async def _owner_role(session: AsyncSession) -> Role:
    role = (
        await session.execute(
            select(Role).where(
                Role.code == "owner", Role.is_system.is_(True), Role.organization_id.is_(None)
            )
        )
    ).scalar_one_or_none()
    if role is None:
        raise NotFoundError("system role 'owner' not found")
    return role


async def _owner_bindings(
    session: AsyncSession, *, space_id: uuid.UUID, organization_id: uuid.UUID, owner_role_id: uuid.UUID
) -> list[RoleBinding]:
    return list(
        (
            await session.execute(
                select(RoleBinding).where(
                    RoleBinding.organization_id == organization_id,
                    RoleBinding.resource_type == "space",
                    RoleBinding.resource_id == space_id,
                    RoleBinding.role_id == owner_role_id,
                )
            )
        )
        .scalars()
        .all()
    )


async def available_owner_ids(
    session: AsyncSession, *, space_id: uuid.UUID, organization_id: uuid.UUID
) -> set[uuid.UUID]:
    """Owners who can act: active people holding the role who are not the organisation owner."""

    role = await _owner_role(session)
    bindings = await _owner_bindings(
        session, space_id=space_id, organization_id=organization_id, owner_role_id=role.id
    )
    user_ids = {b.subject_id for b in bindings if b.subject_type == "user"}
    if not user_ids:
        return set()
    active = set(
        (
            await session.execute(
                select(User.id).where(
                    User.id.in_(user_ids),
                    User.organization_id == organization_id,
                    User.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    available: set[uuid.UUID] = set()
    for uid in active:
        if not await access_guard.user_is_restricted_owner(
            session, organization_id=organization_id, user_id=uid
        ):
            available.add(uid)
    return available


async def owners_unavailable_for_spaces(
    session: AsyncSession, *, organization_id: uuid.UUID, space_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """The given spaces that have no available owner (for the admin console's warning)."""

    return {
        sid
        for sid in space_ids
        if not await available_owner_ids(session, space_id=sid, organization_id=organization_id)
    }


async def transfer_owner(
    session: AsyncSession,
    *,
    space_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    new_owner_user_id: uuid.UUID,
) -> OwnerTransfer:
    async with session.begin():
        guard = await access_guard.watch(
            session, actor_user_id=actor_user_id, organization_id=organization_id
        )
        space = (
            await session.execute(
                select(Space).where(
                    Space.id == space_id, Space.organization_id == organization_id
                )
            )
        ).scalar_one_or_none()
        if space is None:
            raise NotFoundError(f"space not found: {space_id}")
        if space.preset == "personal":
            raise InvalidInputError("a personal space cannot change owner")
        if space.status != "active":
            raise InvalidInputError("an archived space cannot change owner")

        available = await available_owner_ids(
            session, space_id=space_id, organization_id=organization_id
        )
        if actor_user_id in available:
            mode = "handover"
        elif not available and await access_guard.is_org_owner(
            session, organization_id=organization_id, user_id=actor_user_id
        ):
            mode = "replacement"
        elif not available:
            # Nobody can act and the caller is not the super administrator.
            raise ForbiddenError(
                "this space has no available owner; the organisation owner can name a new one"
            )
        else:
            # An owner is available: only they decide who takes over.
            raise ForbiddenError("only an owner of this space can hand it over")

        target = (
            await session.execute(
                select(User).where(
                    User.id == new_owner_user_id, User.organization_id == organization_id
                )
            )
        ).scalar_one_or_none()
        if target is None:
            raise NotFoundError(f"user not found in this organisation: {new_owner_user_id}")
        if target.status != "active":
            raise InvalidInputError("the new owner must be an active member of the organisation")
        if await access_guard.user_is_restricted_owner(
            session, organization_id=organization_id, user_id=target.id
        ):
            raise ForbiddenError(access_guard.OWNER_CANNOT_MANAGE_SPACES_MESSAGE)

        role = await _owner_role(session)
        owner_bindings = await _owner_bindings(
            session, space_id=space_id, organization_id=organization_id, owner_role_id=role.id
        )
        removed: list[uuid.UUID] = []
        for binding in owner_bindings:
            if binding.subject_type != "user" or binding.subject_id == target.id:
                continue
            if mode == "replacement" or binding.subject_id == actor_user_id:
                removed.append(binding.subject_id)
                await session.delete(binding)
        if not any(b.subject_type == "user" and b.subject_id == target.id for b in owner_bindings):
            session.add(
                RoleBinding(
                    organization_id=organization_id,
                    role_id=role.id,
                    subject_type="user",
                    subject_id=target.id,
                    resource_type="space",
                    resource_id=space_id,
                    conditions={},
                    created_by_user_id=actor_user_id,
                )
            )
        if mode == "handover" and actor_user_id in removed:
            # The previous owner keeps editing access.
            editor = (
                await session.execute(
                    select(Role).where(
                        Role.code == "editor", Role.is_system.is_(True), Role.organization_id.is_(None)
                    )
                )
            ).scalar_one()
            session.add(
                RoleBinding(
                    organization_id=organization_id,
                    role_id=editor.id,
                    subject_type="user",
                    subject_id=actor_user_id,
                    resource_type="space",
                    resource_id=space_id,
                    conditions={},
                    created_by_user_id=actor_user_id,
                )
            )
        if space.owner_type == "user":
            space.owner_id = target.id
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="space.owner_transferred",
                resource_type="space",
                resource_id=space_id,
                space_id=space_id,
                payload={
                    "mode": mode,
                    "new_owner_user_id": str(target.id),
                    "new_owner_name": target.display_name,
                    "removed_owner_user_ids": [str(u) for u in removed],
                },
            )
        )
        await session.flush()
        await guard.check()
    return OwnerTransfer(
        space_id=space_id,
        new_owner_user_id=target.id,
        mode=mode,
        removed_owner_user_ids=removed,
    )
