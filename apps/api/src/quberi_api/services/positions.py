"""Positions (job roles) service.

Scope
-----
* **Definition CRUD** — create / update / archive positions. Archive refuses
  to hide a position that still has current occupants (matches the org-unit
  archive semantics).
* **Occupancy** — assign a user to a position (with optional
  ``is_primary`` and explicit ``start_date`` / ``end_date``), end a tenure,
  and toggle which position is a user's current primary.
* **Reporting line** — ``list_direct_reports(position_id)`` returns users
  who currently occupy positions whose ``reports_to_position_id`` points at
  the given position; ``list_reporting_line(user_id)`` returns the users
  above the given user on the org chart (through their primary position's
  ``reports_to``).

Every mutation writes an ``AuditEvent`` and refuses if the row is mirrored
from an external directory (``sync_state='synced'``).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.identity.models import User
from quberi_api.domain.org.models import OrganizationUnit
from quberi_api.domain.positions.models import Position, UserPosition
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
class PositionView:
    """A position row + a cheap current-occupancy count."""

    position: Position
    current_occupants: int


@dataclass(frozen=True)
class PositionOccupantView:
    """A user currently (or historically) occupying a position."""

    user: User
    tenure_id: uuid.UUID
    is_primary: bool
    start_date: date
    end_date: Optional[date]


class OccupantsRemainError(ServiceError):
    """Raised when trying to archive a position that still has occupants."""


class HeadcountExceededError(ServiceError):
    """Raised when assigning a user would exceed the position's budget."""


class FrozenPositionError(ServiceError):
    """Raised when trying to assign a user to a frozen position."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,62}[A-Za-z0-9]$|^[A-Za-z0-9]$")


def _validate_name(name: str) -> str:
    trimmed = name.strip()
    if not trimmed:
        raise InvalidInputError("position name is required")
    if len(trimmed) > 200:
        raise InvalidInputError("position name exceeds 200 characters")
    return trimmed


def _validate_code(code: Optional[str]) -> Optional[str]:
    if code is None:
        return None
    trimmed = code.strip()
    if not trimmed:
        return None
    if not _CODE_RE.match(trimmed):
        raise InvalidInputError(
            "position code must be 1-64 chars of letters, digits, '.', '_', or '-'"
        )
    return trimmed


def _validate_headcount(value: int) -> int:
    if value < 0:
        raise InvalidInputError("headcount_budget must be >= 0 (0 = unlimited)")
    return value


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


async def _load_position(
    session: AsyncSession,
    *,
    position_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> Position:
    stmt = select(Position).where(
        Position.id == position_id,
        Position.organization_id == organization_id,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"position not found: {position_id}")
    return row


async def _count_current_occupants(
    session: AsyncSession, *, position_id: uuid.UUID
) -> int:
    from sqlalchemy import func

    stmt = (
        select(func.count())
        .select_from(UserPosition)
        .where(UserPosition.position_id == position_id)
        .where(UserPosition.end_date.is_(None))
    )
    return int((await session.execute(stmt)).scalar_one() or 0)


async def _validate_ancestor_chain(
    session: AsyncSession,
    *,
    position_id: uuid.UUID,
    proposed_reports_to: Optional[uuid.UUID],
    organization_id: uuid.UUID,
) -> None:
    """Walking ``reports_to_position_id`` from ``proposed_reports_to`` must
    not lead back to ``position_id`` (that would form a cycle).
    """

    if proposed_reports_to is None or proposed_reports_to == position_id:
        if proposed_reports_to == position_id:
            raise InvalidInputError("a position cannot report to itself")
        return

    seen: set[uuid.UUID] = {position_id}
    cursor: Optional[uuid.UUID] = proposed_reports_to
    while cursor is not None:
        if cursor in seen:
            raise InvalidInputError(
                "reports_to chain forms a cycle back to this position"
            )
        seen.add(cursor)
        parent_stmt = select(Position.reports_to_position_id).where(
            Position.id == cursor,
            Position.organization_id == organization_id,
        )
        cursor = (await session.execute(parent_stmt)).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_positions(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    include_archived: bool = False,
) -> list[PositionView]:
    stmt = (
        select(Position)
        .where(Position.organization_id == organization_id)
        .order_by(Position.name.asc())
    )
    if not include_archived:
        stmt = stmt.where(Position.status != "archived")
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        PositionView(
            position=row,
            current_occupants=await _count_current_occupants(session, position_id=row.id),
        )
        for row in rows
    ]


async def get_position(
    session: AsyncSession,
    *,
    position_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> PositionView:
    position = await _load_position(
        session, position_id=position_id, organization_id=organization_id
    )
    count = await _count_current_occupants(session, position_id=position.id)
    return PositionView(position=position, current_occupants=count)


async def list_occupants(
    session: AsyncSession,
    *,
    position_id: uuid.UUID,
    organization_id: uuid.UUID,
    include_ended: bool = False,
) -> list[PositionOccupantView]:
    # Confirm the position exists in this tenant.
    await _load_position(
        session, position_id=position_id, organization_id=organization_id
    )
    stmt = (
        select(UserPosition, User)
        .join(User, User.id == UserPosition.user_id)
        .where(UserPosition.position_id == position_id)
        .order_by(
            UserPosition.is_primary.desc(),
            UserPosition.end_date.asc().nullsfirst(),
            User.display_name.asc(),
        )
    )
    if not include_ended:
        stmt = stmt.where(UserPosition.end_date.is_(None))
    rows = (await session.execute(stmt)).all()
    return [
        PositionOccupantView(
            user=user,
            tenure_id=link.id,
            is_primary=link.is_primary,
            start_date=link.start_date,
            end_date=link.end_date,
        )
        for link, user in rows
    ]


async def list_positions_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    include_ended: bool = False,
) -> list[tuple[Position, UserPosition]]:
    stmt = (
        select(Position, UserPosition)
        .join(UserPosition, UserPosition.position_id == Position.id)
        .where(UserPosition.user_id == user_id)
        .where(UserPosition.organization_id == organization_id)
        .order_by(UserPosition.is_primary.desc(), Position.name.asc())
    )
    if not include_ended:
        stmt = stmt.where(UserPosition.end_date.is_(None))
    return [(pos, link) for pos, link in (await session.execute(stmt)).all()]


async def list_direct_reports(
    session: AsyncSession,
    *,
    position_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> list[User]:
    """Users currently occupying positions whose ``reports_to`` = ``position_id``."""

    await _load_position(
        session, position_id=position_id, organization_id=organization_id
    )
    stmt = (
        select(User)
        .join(UserPosition, UserPosition.user_id == User.id)
        .join(Position, Position.id == UserPosition.position_id)
        .where(Position.reports_to_position_id == position_id)
        .where(UserPosition.end_date.is_(None))
        .distinct()
        .order_by(User.display_name.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_reporting_line(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> list[User]:
    """Users occupying the position(s) directly above the given user.

    We walk from the user's *primary* current position → its
    ``reports_to_position_id`` → the users currently occupying that
    position. Returns an empty list when the user has no primary position
    or when the primary position is a root (no reports_to).
    """

    primary_stmt = (
        select(Position.reports_to_position_id)
        .join(UserPosition, UserPosition.position_id == Position.id)
        .where(UserPosition.user_id == user_id)
        .where(UserPosition.organization_id == organization_id)
        .where(UserPosition.is_primary.is_(True))
        .where(UserPosition.end_date.is_(None))
        .limit(1)
    )
    manager_position_id = (await session.execute(primary_stmt)).scalar_one_or_none()
    if manager_position_id is None:
        return []

    stmt = (
        select(User)
        .join(UserPosition, UserPosition.user_id == User.id)
        .where(UserPosition.position_id == manager_position_id)
        .where(UserPosition.end_date.is_(None))
        .distinct()
        .order_by(User.display_name.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Mutations — position definitions
# ---------------------------------------------------------------------------


async def create_position(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    name: str,
    code: Optional[str] = None,
    organization_unit_id: Optional[uuid.UUID] = None,
    reports_to_position_id: Optional[uuid.UUID] = None,
    job_family: Optional[str] = None,
    grade: Optional[str] = None,
    headcount_budget: int = 1,
    description: Optional[str] = None,
    attributes: Optional[dict] = None,
) -> PositionView:
    validated_name = _validate_name(name)
    validated_code = _validate_code(code)
    validated_budget = _validate_headcount(headcount_budget)

    async with session.begin():
        if organization_unit_id is not None:
            ou_stmt = select(OrganizationUnit.id).where(
                OrganizationUnit.id == organization_unit_id,
                OrganizationUnit.organization_id == organization_id,
            )
            if (await session.execute(ou_stmt)).scalar_one_or_none() is None:
                raise InvalidInputError(
                    f"organization unit not found: {organization_unit_id}"
                )
        if reports_to_position_id is not None:
            await _load_position(
                session,
                position_id=reports_to_position_id,
                organization_id=organization_id,
            )

        position = Position(
            organization_id=organization_id,
            name=validated_name,
            code=validated_code,
            organization_unit_id=organization_unit_id,
            reports_to_position_id=reports_to_position_id,
            job_family=(job_family or None),
            grade=(grade or None),
            headcount_budget=validated_budget,
            description=(description or None),
            status="active",
            attributes=attributes or {},
            sync_state="local",
        )
        session.add(position)
        try:
            await session.flush()
        except Exception as exc:  # noqa: BLE001
            raise AlreadyExistsError(
                f"position with code {validated_code!r} already exists in this organisation"
            ) from exc

        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="position.created",
                resource_type="position",
                resource_id=position.id,
                payload={
                    "name": validated_name,
                    "code": validated_code,
                    "reports_to": (
                        str(reports_to_position_id) if reports_to_position_id else None
                    ),
                    "org_unit_id": (
                        str(organization_unit_id) if organization_unit_id else None
                    ),
                    "headcount_budget": validated_budget,
                },
            )
        )
        await session.flush()
        await session.refresh(position, attribute_names=["created_at", "updated_at"])

    return PositionView(position=position, current_occupants=0)


async def update_position(
    session: AsyncSession,
    *,
    position_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    name: Optional[str] = None,
    code: Optional[str] = None,
    organization_unit_id: Optional[uuid.UUID] = None,
    clear_org_unit: bool = False,
    reports_to_position_id: Optional[uuid.UUID] = None,
    clear_reports_to: bool = False,
    job_family: Optional[str] = None,
    grade: Optional[str] = None,
    headcount_budget: Optional[int] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    attributes: Optional[dict] = None,
) -> PositionView:
    async with session.begin():
        position = await _load_position(
            session, position_id=position_id, organization_id=organization_id
        )
        if position.sync_state == "synced":
            raise InvalidInputError(
                "position is mirrored from an external directory and cannot be edited here"
            )

        changes: dict[str, object] = {}
        if name is not None:
            changes["name"] = _validate_name(name)
        if code is not None:
            changes["code"] = _validate_code(code)
        if clear_org_unit:
            changes["organization_unit_id"] = None
        elif organization_unit_id is not None:
            ou_stmt = select(OrganizationUnit.id).where(
                OrganizationUnit.id == organization_unit_id,
                OrganizationUnit.organization_id == organization_id,
            )
            if (await session.execute(ou_stmt)).scalar_one_or_none() is None:
                raise InvalidInputError(
                    f"organization unit not found: {organization_unit_id}"
                )
            changes["organization_unit_id"] = organization_unit_id
        if clear_reports_to:
            changes["reports_to_position_id"] = None
        elif reports_to_position_id is not None:
            await _validate_ancestor_chain(
                session,
                position_id=position.id,
                proposed_reports_to=reports_to_position_id,
                organization_id=organization_id,
            )
            await _load_position(
                session,
                position_id=reports_to_position_id,
                organization_id=organization_id,
            )
            changes["reports_to_position_id"] = reports_to_position_id
        if job_family is not None:
            changes["job_family"] = job_family.strip() or None
        if grade is not None:
            changes["grade"] = grade.strip() or None
        if headcount_budget is not None:
            new_budget = _validate_headcount(headcount_budget)
            current = await _count_current_occupants(session, position_id=position.id)
            if new_budget != 0 and new_budget < current:
                raise InvalidInputError(
                    f"headcount_budget ({new_budget}) is below current occupants "
                    f"({current}); end their tenures first"
                )
            changes["headcount_budget"] = new_budget
        if description is not None:
            changes["description"] = description or None
        if status is not None:
            if status not in ("active", "frozen"):
                raise InvalidInputError(
                    "status may only be set to 'active' or 'frozen' here; "
                    "use archive_position to retire a position"
                )
            changes["status"] = status
        if attributes is not None:
            changes["attributes"] = attributes

        if not changes:
            return PositionView(
                position=position,
                current_occupants=await _count_current_occupants(
                    session, position_id=position.id
                ),
            )

        for field, value in changes.items():
            setattr(position, field, value)
        try:
            await session.flush()
        except Exception as exc:  # noqa: BLE001
            raise AlreadyExistsError(
                "position code conflicts with another position in this organisation"
            ) from exc

        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="position.updated",
                resource_type="position",
                resource_id=position.id,
                payload={"changes": {k: str(v) for k, v in changes.items()}},
            )
        )
        await session.flush()
        await session.refresh(position, attribute_names=["updated_at"])

    return PositionView(
        position=position,
        current_occupants=await _count_current_occupants(session, position_id=position.id),
    )


async def archive_position(
    session: AsyncSession,
    *,
    position_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> PositionView:
    async with session.begin():
        position = await _load_position(
            session, position_id=position_id, organization_id=organization_id
        )
        if position.sync_state == "synced":
            raise InvalidInputError(
                "position is mirrored from an external directory; archive it there"
            )
        if position.status == "archived":
            count = await _count_current_occupants(session, position_id=position.id)
            return PositionView(position=position, current_occupants=count)

        current = await _count_current_occupants(session, position_id=position.id)
        if current > 0:
            raise OccupantsRemainError(
                f"end all {current} current tenures before archiving this position"
            )

        position.status = "archived"
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="position.archived",
                resource_type="position",
                resource_id=position.id,
                payload={"name": position.name},
            )
        )
        await session.flush()
        await session.refresh(position, attribute_names=["updated_at"])

    return PositionView(position=position, current_occupants=0)


# ---------------------------------------------------------------------------
# Mutations — occupancy
# ---------------------------------------------------------------------------


async def assign_user_to_position(
    session: AsyncSession,
    *,
    position_id: uuid.UUID,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    is_primary: bool = False,
    start_date_: Optional[date] = None,
) -> PositionOccupantView:
    """Assign ``user`` to ``position``. Fails if the position is frozen /
    archived, if the headcount budget is exceeded, or if the user already
    has an active tenure in this position.
    """

    async with session.begin():
        position = await _load_position(
            session, position_id=position_id, organization_id=organization_id
        )
        if position.status == "archived":
            raise InvalidInputError("cannot assign to an archived position")
        if position.status == "frozen":
            raise FrozenPositionError(
                "position is frozen; unfreeze it before adding new occupants"
            )

        user_stmt = select(User).where(
            User.id == user_id, User.organization_id == organization_id
        )
        user = (await session.execute(user_stmt)).scalar_one_or_none()
        if user is None:
            raise NotFoundError(f"user not found in this organisation: {user_id}")

        # Uniqueness of current tenure per (user, position) is enforced by
        # the partial index; we surface a clean error preemptively.
        existing_stmt = select(UserPosition).where(
            UserPosition.user_id == user_id,
            UserPosition.position_id == position_id,
            UserPosition.end_date.is_(None),
        )
        if (await session.execute(existing_stmt)).scalar_one_or_none() is not None:
            raise AlreadyExistsError(
                "user already occupies this position"
            )

        # Enforce headcount budget (0 = unlimited).
        if position.headcount_budget > 0:
            current = await _count_current_occupants(
                session, position_id=position.id
            )
            if current >= position.headcount_budget:
                raise HeadcountExceededError(
                    f"headcount budget ({position.headcount_budget}) already met "
                    "for this position"
                )

        if is_primary:
            await _demote_existing_primary_position(
                session, user_id=user_id, organization_id=organization_id
            )

        link = UserPosition(
            organization_id=organization_id,
            user_id=user_id,
            position_id=position_id,
            is_primary=is_primary,
            start_date=start_date_ or date.today(),
            end_date=None,
            attributes={},
            added_by_user_id=actor_user_id,
        )
        session.add(link)

        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="position.assigned",
                resource_type="position",
                resource_id=position_id,
                payload={
                    "user_id": str(user_id),
                    "is_primary": is_primary,
                    "start_date": (start_date_ or date.today()).isoformat(),
                },
            )
        )
        await session.flush()
        await session.refresh(link, attribute_names=["created_at", "updated_at"])

    return PositionOccupantView(
        user=user,
        tenure_id=link.id,
        is_primary=link.is_primary,
        start_date=link.start_date,
        end_date=link.end_date,
    )


async def end_position_tenure(
    session: AsyncSession,
    *,
    position_id: uuid.UUID,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    end_date_: Optional[date] = None,
) -> None:
    """End a user's current tenure in a position (soft close, keeps history)."""

    async with session.begin():
        stmt = select(UserPosition).where(
            UserPosition.user_id == user_id,
            UserPosition.position_id == position_id,
            UserPosition.organization_id == organization_id,
            UserPosition.end_date.is_(None),
        )
        link = (await session.execute(stmt)).scalar_one_or_none()
        if link is None:
            raise NotFoundError("no active tenure to end for this (user, position)")

        link.end_date = end_date_ or date.today()
        # ``is_primary`` can survive as historical data — but the partial
        # unique index only cares about currently-active primary rows.
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="position.ended",
                resource_type="position",
                resource_id=position_id,
                payload={
                    "user_id": str(user_id),
                    "end_date": link.end_date.isoformat(),
                    "was_primary": link.is_primary,
                },
            )
        )


async def set_primary_position(
    session: AsyncSession,
    *,
    position_id: uuid.UUID,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> PositionOccupantView:
    """Mark this position as the user's *current* primary. Requires an
    existing active tenure for (user, position).
    """

    async with session.begin():
        link_stmt = select(UserPosition).where(
            UserPosition.user_id == user_id,
            UserPosition.position_id == position_id,
            UserPosition.organization_id == organization_id,
            UserPosition.end_date.is_(None),
        )
        link = (await session.execute(link_stmt)).scalar_one_or_none()
        if link is None:
            raise NotFoundError(
                "assign the user to this position first before marking it primary"
            )
        user = await session.get(User, user_id)
        if user is None or user.organization_id != organization_id:
            raise NotFoundError(f"user not found in this organisation: {user_id}")

        await _demote_existing_primary_position(
            session, user_id=user_id, organization_id=organization_id
        )
        link.is_primary = True
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="position.primary_set",
                resource_type="position",
                resource_id=position_id,
                payload={"user_id": str(user_id)},
            )
        )
        await session.flush()

    return PositionOccupantView(
        user=user,
        tenure_id=link.id,
        is_primary=True,
        start_date=link.start_date,
        end_date=link.end_date,
    )


async def _demote_existing_primary_position(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> None:
    stmt = (
        update(UserPosition)
        .where(UserPosition.user_id == user_id)
        .where(UserPosition.organization_id == organization_id)
        .where(UserPosition.end_date.is_(None))
        .where(UserPosition.is_primary.is_(True))
        .values(is_primary=False)
    )
    await session.execute(stmt)
