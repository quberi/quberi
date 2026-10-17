"""Organisation-unit lifecycle and membership.

Split into two logical halves:

1. **Tree operations** — create/rename/move/archive an ``OrganizationUnit``.
   Move enforces "no self-parent and no descendant-parent" so we never form
   a cycle. Archive refuses to soft-delete a node that still has active
   children; the caller is expected to archive or move them first.

2. **Membership** — a ``User`` may belong to zero or more units. At most
   one of those may carry ``is_primary=True`` (enforced by the partial
   unique index installed in migration 0005). ``assign`` with
   ``make_primary=True`` demotes the previous primary in the same txn.

Every mutation records an ``AuditEvent`` so we do not need to add ad-hoc
logging around the callers.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.enums import ORG_UNIT_TYPES
from quberi_api.domain.identity.models import User
from quberi_api.domain.org.models import OrganizationUnit, UserOrganizationUnit
from quberi_api.services import access_guard
from quberi_api.services.errors import (
    AlreadyExistsError,
    InvalidInputError,
    NotFoundError,
    ServiceError,
)

# ---------------------------------------------------------------------------
# Result payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrgUnitView:
    """Flat row + a pre-computed ancestor path (list of parent ids, root first)."""

    unit: OrganizationUnit
    ancestor_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class OrgUnitMemberView:
    """A user within a unit + whether this is their primary unit."""

    user: User
    is_primary: bool
    membership_id: uuid.UUID


# ---------------------------------------------------------------------------
# Constants / validation
# ---------------------------------------------------------------------------

_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,62}[A-Za-z0-9]$|^[A-Za-z0-9]$")
_ORG_UNIT_TYPES: frozenset[str] = frozenset(ORG_UNIT_TYPES)


class CycleError(ServiceError):
    """Raised when a move would form a cycle."""


class HasChildrenError(ServiceError):
    """Raised when archiving a unit that still has active children."""


def _validate_name(name: str) -> str:
    trimmed = name.strip()
    if not trimmed:
        raise InvalidInputError("org unit name is required")
    if len(trimmed) > 200:
        raise InvalidInputError("org unit name exceeds 200 characters")
    return trimmed


def _validate_code(code: str | None) -> Optional[str]:
    if code is None:
        return None
    trimmed = code.strip()
    if not trimmed:
        return None
    if not _CODE_RE.match(trimmed):
        raise InvalidInputError(
            "org unit code must be 1-64 chars of letters, digits, '.', '_', or '-'"
        )
    return trimmed


def _validate_type(kind: str) -> str:
    if kind not in _ORG_UNIT_TYPES:
        raise InvalidInputError(
            f"unknown org unit type: {kind!r} (allowed: {sorted(_ORG_UNIT_TYPES)})"
        )
    return kind


# ---------------------------------------------------------------------------
# Read helpers (used inside transactions)
# ---------------------------------------------------------------------------


async def _load_unit(
    session: AsyncSession,
    *,
    unit_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> OrganizationUnit:
    stmt = select(OrganizationUnit).where(
        OrganizationUnit.id == unit_id,
        OrganizationUnit.organization_id == organization_id,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"org unit not found: {unit_id}")
    return row


async def _ancestor_chain(
    session: AsyncSession,
    *,
    unit: OrganizationUnit,
) -> tuple[uuid.UUID, ...]:
    """Return the chain of parent ids, root-first (excludes the unit itself)."""

    chain: list[uuid.UUID] = []
    seen: set[uuid.UUID] = {unit.id}
    cursor = unit.parent_id
    while cursor is not None:
        if cursor in seen:  # defensive; the DB should prevent this
            break
        seen.add(cursor)
        chain.append(cursor)
        parent_stmt = select(OrganizationUnit.parent_id).where(
            OrganizationUnit.id == cursor
        )
        cursor = (await session.execute(parent_stmt)).scalar_one_or_none()
    chain.reverse()
    return tuple(chain)


async def _descendant_ids(
    session: AsyncSession,
    *,
    unit_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> set[uuid.UUID]:
    """Return every descendant id (recursive) of ``unit_id``.

    Uses a recursive CTE so a deep tree does not blow the Python stack.
    """

    cte = (
        select(OrganizationUnit.id)
        .where(OrganizationUnit.parent_id == unit_id)
        .where(OrganizationUnit.organization_id == organization_id)
        .cte(recursive=True)
    )
    child_alias = cte.alias("child")
    cte = cte.union_all(
        select(OrganizationUnit.id)
        .join(child_alias, OrganizationUnit.parent_id == child_alias.c.id)
        .where(OrganizationUnit.organization_id == organization_id)
    )
    rows = await session.execute(select(cte.c.id))
    return set(rows.scalars().all())


# ---------------------------------------------------------------------------
# Public reads
# ---------------------------------------------------------------------------


async def list_tree(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    include_archived: bool = False,
) -> list[OrgUnitView]:
    stmt = (
        select(OrganizationUnit)
        .where(OrganizationUnit.organization_id == organization_id)
        .order_by(OrganizationUnit.name.asc())
    )
    if not include_archived:
        stmt = stmt.where(OrganizationUnit.status == "active")
    rows = list((await session.execute(stmt)).scalars().all())

    # Build ancestor path per row from an in-memory parent map. Avoids N SELECTs
    # while remaining correct for arbitrarily deep trees.
    parent_by_id = {row.id: row.parent_id for row in rows}
    views: list[OrgUnitView] = []
    for row in rows:
        chain: list[uuid.UUID] = []
        cursor = row.parent_id
        while cursor is not None and cursor in parent_by_id and cursor not in chain:
            chain.append(cursor)
            cursor = parent_by_id.get(cursor)
        chain.reverse()
        views.append(OrgUnitView(unit=row, ancestor_ids=tuple(chain)))
    return views


async def get_org_unit(
    session: AsyncSession,
    *,
    unit_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> OrgUnitView:
    unit = await _load_unit(session, unit_id=unit_id, organization_id=organization_id)
    ancestors = await _ancestor_chain(session, unit=unit)
    return OrgUnitView(unit=unit, ancestor_ids=ancestors)


async def list_members_of_unit(
    session: AsyncSession,
    *,
    unit_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> list[OrgUnitMemberView]:
    # Confirm the unit exists in this org so we return NotFoundError rather
    # than an empty list for a bad id.
    await _load_unit(session, unit_id=unit_id, organization_id=organization_id)

    stmt = (
        select(User, UserOrganizationUnit)
        .join(UserOrganizationUnit, UserOrganizationUnit.user_id == User.id)
        .where(UserOrganizationUnit.organization_unit_id == unit_id)
        .where(UserOrganizationUnit.organization_id == organization_id)
        .order_by(UserOrganizationUnit.is_primary.desc(), User.display_name.asc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        OrgUnitMemberView(user=user, is_primary=link.is_primary, membership_id=link.id)
        for user, link in rows
    ]


async def list_units_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> list[OrgUnitMemberView]:
    stmt = (
        select(User, UserOrganizationUnit, OrganizationUnit)
        .join(UserOrganizationUnit, UserOrganizationUnit.user_id == User.id)
        .join(
            OrganizationUnit,
            OrganizationUnit.id == UserOrganizationUnit.organization_unit_id,
        )
        .where(User.id == user_id)
        .where(User.organization_id == organization_id)
        .order_by(UserOrganizationUnit.is_primary.desc(), OrganizationUnit.name.asc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        OrgUnitMemberView(user=user, is_primary=link.is_primary, membership_id=link.id)
        for user, link, _ in rows
    ]


# ---------------------------------------------------------------------------
# Mutations — tree operations
# ---------------------------------------------------------------------------


async def create_org_unit(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    name: str,
    type: str = "department",
    parent_id: Optional[uuid.UUID] = None,
    code: Optional[str] = None,
    manager_user_id: Optional[uuid.UUID] = None,
    attributes: Optional[dict] = None,
) -> OrgUnitView:
    validated_name = _validate_name(name)
    validated_type = _validate_type(type)
    validated_code = _validate_code(code)

    async with session.begin():
        if parent_id is not None:
            # Ensure the parent belongs to the same tenant.
            await _load_unit(
                session, unit_id=parent_id, organization_id=organization_id
            )
        if manager_user_id is not None:
            manager_stmt = select(User.id).where(
                User.id == manager_user_id,
                User.organization_id == organization_id,
            )
            if (await session.execute(manager_stmt)).scalar_one_or_none() is None:
                raise InvalidInputError(
                    f"manager user not found in this organisation: {manager_user_id}"
                )

        unit = OrganizationUnit(
            organization_id=organization_id,
            parent_id=parent_id,
            type=validated_type,
            name=validated_name,
            code=validated_code,
            manager_user_id=manager_user_id,
            status="active",
            attributes=attributes or {},
            sync_state="local",
        )
        session.add(unit)
        try:
            await session.flush()
        except Exception as exc:  # noqa: BLE001
            # Most likely the (organization, parent, code) unique constraint
            # tripped. Surface a clean 409 rather than raw IntegrityError.
            raise AlreadyExistsError(
                f"org unit with code {validated_code!r} already exists under this parent"
            ) from exc

        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="org_unit.created",
                resource_type="organization_unit",
                resource_id=unit.id,
                payload={
                    "name": validated_name,
                    "type": validated_type,
                    "parent_id": str(parent_id) if parent_id else None,
                    "code": validated_code,
                },
            )
        )
        await session.flush()
        await session.refresh(unit, attribute_names=["created_at", "updated_at"])

    return OrgUnitView(unit=unit, ancestor_ids=())


async def update_org_unit(
    session: AsyncSession,
    *,
    unit_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    name: Optional[str] = None,
    type: Optional[str] = None,
    code: Optional[str] = None,  # empty string clears
    manager_user_id: Optional[uuid.UUID] = None,  # explicit ``None`` = keep
    clear_manager: bool = False,
    attributes: Optional[dict] = None,
) -> OrgUnitView:
    async with session.begin():
        unit = await _load_unit(
            session, unit_id=unit_id, organization_id=organization_id
        )
        if unit.sync_state == "synced":
            raise InvalidInputError(
                "unit is mirrored from an external directory and cannot be edited "
                "here — modify it in the source directory instead"
            )

        changes: dict[str, object] = {}
        if name is not None:
            changes["name"] = _validate_name(name)
        if type is not None:
            changes["type"] = _validate_type(type)
        if code is not None:
            changes["code"] = _validate_code(code)
        if clear_manager:
            changes["manager_user_id"] = None
        elif manager_user_id is not None:
            manager_stmt = select(User.id).where(
                User.id == manager_user_id,
                User.organization_id == organization_id,
            )
            if (await session.execute(manager_stmt)).scalar_one_or_none() is None:
                raise InvalidInputError(
                    f"manager user not found in this organisation: {manager_user_id}"
                )
            changes["manager_user_id"] = manager_user_id
        if attributes is not None:
            changes["attributes"] = attributes

        if not changes:
            # Nothing to do; return the current state without emitting audit.
            ancestors = await _ancestor_chain(session, unit=unit)
            return OrgUnitView(unit=unit, ancestor_ids=ancestors)

        for field, value in changes.items():
            setattr(unit, field, value)
        try:
            await session.flush()
        except Exception as exc:  # noqa: BLE001
            raise AlreadyExistsError(
                "org unit code conflicts with a sibling under the same parent"
            ) from exc

        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="org_unit.updated",
                resource_type="organization_unit",
                resource_id=unit.id,
                payload={"changes": {k: str(v) for k, v in changes.items()}},
            )
        )
        await session.flush()
        await session.refresh(unit, attribute_names=["updated_at"])
        ancestors = await _ancestor_chain(session, unit=unit)

    return OrgUnitView(unit=unit, ancestor_ids=ancestors)


async def move_org_unit(
    session: AsyncSession,
    *,
    unit_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    new_parent_id: Optional[uuid.UUID],
) -> OrgUnitView:
    """Reparent a unit. ``new_parent_id=None`` promotes it to the root."""
    async with session.begin():
        guard = await access_guard.watch(
            session, actor_user_id=actor_user_id, organization_id=organization_id
        )
        unit = await _load_unit(
            session, unit_id=unit_id, organization_id=organization_id
        )
        if unit.sync_state == "synced":
            raise InvalidInputError(
                "unit is mirrored from an external directory and cannot be moved here"
            )
        if new_parent_id == unit.id:
            raise CycleError("a unit cannot be its own parent")

        if new_parent_id is not None:
            new_parent = await _load_unit(
                session, unit_id=new_parent_id, organization_id=organization_id
            )
            descendants = await _descendant_ids(
                session, unit_id=unit.id, organization_id=organization_id
            )
            if new_parent.id in descendants:
                raise CycleError(
                    "cannot move a unit under one of its own descendants"
                )

        old_parent_id = unit.parent_id
        unit.parent_id = new_parent_id
        try:
            await session.flush()
        except Exception as exc:  # noqa: BLE001
            raise AlreadyExistsError(
                "org unit code conflicts with a sibling under the new parent"
            ) from exc

        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="org_unit.moved",
                resource_type="organization_unit",
                resource_id=unit.id,
                payload={
                    "old_parent_id": str(old_parent_id) if old_parent_id else None,
                    "new_parent_id": str(new_parent_id) if new_parent_id else None,
                },
            )
        )
        await session.flush()
        await guard.check()
        await session.refresh(unit, attribute_names=["updated_at"])
        ancestors = await _ancestor_chain(session, unit=unit)

    return OrgUnitView(unit=unit, ancestor_ids=ancestors)


async def archive_org_unit(
    session: AsyncSession,
    *,
    unit_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> OrgUnitView:
    async with session.begin():
        unit = await _load_unit(
            session, unit_id=unit_id, organization_id=organization_id
        )
        if unit.sync_state == "synced":
            raise InvalidInputError(
                "unit is mirrored from an external directory; archive it there"
            )
        if unit.status == "archived":
            ancestors = await _ancestor_chain(session, unit=unit)
            return OrgUnitView(unit=unit, ancestor_ids=ancestors)

        active_child_stmt = (
            select(OrganizationUnit.id)
            .where(OrganizationUnit.parent_id == unit.id)
            .where(OrganizationUnit.organization_id == organization_id)
            .where(OrganizationUnit.status == "active")
            .limit(1)
        )
        if (await session.execute(active_child_stmt)).scalar_one_or_none() is not None:
            raise HasChildrenError(
                "archive or move the child units first before archiving this one"
            )

        unit.status = "archived"

        # Drop memberships to keep counts honest; audit each removal.
        detach_stmt = (
            select(UserOrganizationUnit)
            .where(UserOrganizationUnit.organization_unit_id == unit.id)
        )
        detached = list((await session.execute(detach_stmt)).scalars().all())
        for link in detached:
            await session.delete(link)

        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="org_unit.archived",
                resource_type="organization_unit",
                resource_id=unit.id,
                payload={"detached_memberships": len(detached)},
            )
        )
        await session.flush()
        await session.refresh(unit, attribute_names=["updated_at"])
        ancestors = await _ancestor_chain(session, unit=unit)

    return OrgUnitView(unit=unit, ancestor_ids=ancestors)


# ---------------------------------------------------------------------------
# Mutations — membership
# ---------------------------------------------------------------------------


async def assign_user_to_unit(
    session: AsyncSession,
    *,
    unit_id: uuid.UUID,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    make_primary: bool = False,
) -> OrgUnitMemberView:
    """Assign ``user`` to ``unit``. ``make_primary=True`` demotes any prior primary."""
    async with session.begin():
        guard = await access_guard.watch(
            session, actor_user_id=actor_user_id, organization_id=organization_id
        )
        unit = await _load_unit(
            session, unit_id=unit_id, organization_id=organization_id
        )
        if unit.status != "active":
            raise InvalidInputError("cannot assign members to an archived unit")

        user_stmt = select(User).where(
            User.id == user_id, User.organization_id == organization_id
        )
        user = (await session.execute(user_stmt)).scalar_one_or_none()
        if user is None:
            raise NotFoundError(f"user not found in this organisation: {user_id}")

        # If already a member, we treat this as (optionally) promoting to
        # primary rather than an error — matches the shape of the UI action.
        existing_stmt = select(UserOrganizationUnit).where(
            UserOrganizationUnit.user_id == user_id,
            UserOrganizationUnit.organization_unit_id == unit_id,
        )
        existing = (await session.execute(existing_stmt)).scalar_one_or_none()

        if existing is None:
            if make_primary:
                await _demote_existing_primary(
                    session,
                    user_id=user_id,
                    organization_id=organization_id,
                )
            link = UserOrganizationUnit(
                organization_id=organization_id,
                user_id=user_id,
                organization_unit_id=unit_id,
                is_primary=make_primary,
                added_by_user_id=actor_user_id,
            )
            session.add(link)
            audit_action = "org_unit.member_added"
        else:
            link = existing
            if make_primary and not existing.is_primary:
                await _demote_existing_primary(
                    session,
                    user_id=user_id,
                    organization_id=organization_id,
                )
                existing.is_primary = True
                audit_action = "org_unit.member_promoted"
            else:
                audit_action = "org_unit.member_touched"

        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action=audit_action,
                resource_type="organization_unit",
                resource_id=unit_id,
                payload={
                    "user_id": str(user_id),
                    "make_primary": make_primary,
                    "is_primary": link.is_primary,
                },
            )
        )
        await session.flush()
        await guard.check()
        await session.refresh(link, attribute_names=["created_at", "updated_at"])
    return OrgUnitMemberView(user=user, is_primary=link.is_primary, membership_id=link.id)


async def remove_user_from_unit(
    session: AsyncSession,
    *,
    unit_id: uuid.UUID,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> None:
    async with session.begin():
        stmt = select(UserOrganizationUnit).where(
            UserOrganizationUnit.user_id == user_id,
            UserOrganizationUnit.organization_unit_id == unit_id,
            UserOrganizationUnit.organization_id == organization_id,
        )
        link = (await session.execute(stmt)).scalar_one_or_none()
        if link is None:
            raise NotFoundError("membership not found")

        await session.delete(link)
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="org_unit.member_removed",
                resource_type="organization_unit",
                resource_id=unit_id,
                payload={"user_id": str(user_id), "was_primary": link.is_primary},
            )
        )


async def set_primary_unit(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    unit_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> OrgUnitMemberView:
    """Mark ``unit_id`` as ``user``'s primary unit (demoting any previous one)."""

    async with session.begin():
        link_stmt = select(UserOrganizationUnit).where(
            UserOrganizationUnit.user_id == user_id,
            UserOrganizationUnit.organization_unit_id == unit_id,
            UserOrganizationUnit.organization_id == organization_id,
        )
        link = (await session.execute(link_stmt)).scalar_one_or_none()
        if link is None:
            raise NotFoundError(
                "assign the user to this unit first before marking it as primary"
            )

        user = await session.get(User, user_id)
        if user is None or user.organization_id != organization_id:
            raise NotFoundError(f"user not found in this organisation: {user_id}")

        await _demote_existing_primary(
            session, user_id=user_id, organization_id=organization_id
        )
        link.is_primary = True

        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="org_unit.primary_set",
                resource_type="organization_unit",
                resource_id=unit_id,
                payload={"user_id": str(user_id)},
            )
        )
        await session.flush()

    return OrgUnitMemberView(user=user, is_primary=True, membership_id=link.id)


async def _demote_existing_primary(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> None:
    """Clear ``is_primary`` on whatever unit the user currently holds primary on."""

    stmt = (
        update(UserOrganizationUnit)
        .where(UserOrganizationUnit.user_id == user_id)
        .where(UserOrganizationUnit.organization_id == organization_id)
        .where(UserOrganizationUnit.is_primary.is_(True))
        .values(is_primary=False)
    )
    await session.execute(stmt)
