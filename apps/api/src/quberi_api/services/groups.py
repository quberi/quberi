"""Group service — matrix cohorts that cut across the org tree.

Scope
-----
* **Definition CRUD** — create / update / archive with the same
  ``sync_state`` guard used elsewhere.
* **Membership** — a member is polymorphic (``user``, ``organization_unit``,
  ``position``, ``service_account``). The service layer enforces:
  (a) the referenced entity exists in the same tenant, and
  (b) duplicate ``(group, member_type, member_id)`` pairs are rejected
  (also enforced by a unique constraint).
* **User → groups view** — ``list_groups_for_user`` returns groups the user
  belongs to via any of the three paths:
  direct user membership, an org unit they're assigned to, a position they
  currently occupy. Ancestor org-unit chains are *not* walked here — group
  membership is literal by design. The permission evaluator walks
  ancestors only for ``RoleBinding`` resolution.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.enums import GROUP_MEMBER_TYPES, GROUP_TYPES
from quberi_api.domain.identity.models import ServiceAccount, User
from quberi_api.domain.org.models import (
    Group,
    GroupMember,
    OrganizationUnit,
    UserOrganizationUnit,
)
from quberi_api.domain.positions.models import Position, UserPosition
from quberi_api.services.errors import (
    AlreadyExistsError,
    InvalidInputError,
    NotFoundError,
)

_GROUP_TYPES: frozenset[str] = frozenset(GROUP_TYPES)
_MEMBER_TYPES: frozenset[str] = frozenset(GROUP_MEMBER_TYPES)


# ---------------------------------------------------------------------------
# Result payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupView:
    group: Group
    member_count: int


@dataclass(frozen=True)
class GroupMemberView:
    """A resolved group member row: raw link + display fields for the target."""

    member: GroupMember
    member_type: str
    member_id: uuid.UUID
    display_name: str
    subtitle: Optional[str] = None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_name(name: str) -> str:
    trimmed = name.strip()
    if not trimmed:
        raise InvalidInputError("group name is required")
    if len(trimmed) > 200:
        raise InvalidInputError("group name exceeds 200 characters")
    return trimmed


def _validate_type(kind: str) -> str:
    if kind not in _GROUP_TYPES:
        raise InvalidInputError(
            f"unknown group type: {kind!r} (allowed: {sorted(_GROUP_TYPES)})"
        )
    return kind


def _validate_member_type(kind: str) -> str:
    if kind not in _MEMBER_TYPES:
        raise InvalidInputError(
            f"unknown group member type: {kind!r} (allowed: {sorted(_MEMBER_TYPES)})"
        )
    return kind


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _load_group(
    session: AsyncSession,
    *,
    group_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> Group:
    stmt = select(Group).where(
        Group.id == group_id,
        Group.organization_id == organization_id,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"group not found: {group_id}")
    return row


async def _member_count(session: AsyncSession, *, group_id: uuid.UUID) -> int:
    stmt = (
        select(func.count())
        .select_from(GroupMember)
        .where(GroupMember.group_id == group_id)
    )
    return int((await session.execute(stmt)).scalar_one() or 0)


async def _resolve_member_target(
    session: AsyncSession,
    *,
    member_type: str,
    member_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> tuple[str, Optional[str]]:
    """Return ``(display_name, subtitle)`` for a member reference — or raise
    ``NotFoundError`` if the target does not exist in the tenant.
    """

    if member_type == "user":
        row = (
            await session.execute(
                select(User.display_name, User.email).where(
                    User.id == member_id,
                    User.organization_id == organization_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError(f"user not found in this organisation: {member_id}")
        return row[0], row[1]

    if member_type == "organization_unit":
        row = (
            await session.execute(
                select(OrganizationUnit.name).where(
                    OrganizationUnit.id == member_id,
                    OrganizationUnit.organization_id == organization_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError(f"org unit not found: {member_id}")
        return row[0], None

    if member_type == "position":
        row = (
            await session.execute(
                select(Position.name, Position.code).where(
                    Position.id == member_id,
                    Position.organization_id == organization_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError(f"position not found: {member_id}")
        return row[0], row[1]

    if member_type == "service_account":
        row = (
            await session.execute(
                select(ServiceAccount.name).where(
                    ServiceAccount.id == member_id,
                    ServiceAccount.organization_id == organization_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError(f"service account not found: {member_id}")
        return row[0], None

    raise InvalidInputError(f"unknown member type: {member_type!r}")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_groups(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    include_archived: bool = False,
) -> list[GroupView]:
    # Compose in a single query: LEFT JOIN aggregate to get counts.
    count_subq = (
        select(GroupMember.group_id, func.count().label("cnt"))
        .group_by(GroupMember.group_id)
        .subquery()
    )
    stmt = (
        select(Group, count_subq.c.cnt)
        .join(count_subq, count_subq.c.group_id == Group.id, isouter=True)
        .where(Group.organization_id == organization_id)
        .order_by(Group.name.asc())
    )
    if not include_archived:
        stmt = stmt.where(Group.status == "active")

    rows = (await session.execute(stmt)).all()
    return [GroupView(group=group, member_count=int(cnt or 0)) for group, cnt in rows]


async def get_group(
    session: AsyncSession,
    *,
    group_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> GroupView:
    group = await _load_group(session, group_id=group_id, organization_id=organization_id)
    return GroupView(
        group=group,
        member_count=await _member_count(session, group_id=group.id),
    )


async def list_members(
    session: AsyncSession,
    *,
    group_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> list[GroupMemberView]:
    """Resolve every member row into (display_name, subtitle) via a single
    query per member type — avoids the classic N+1.
    """

    await _load_group(session, group_id=group_id, organization_id=organization_id)

    rows = list(
        (
            await session.execute(
                select(GroupMember)
                .where(GroupMember.group_id == group_id)
                .order_by(GroupMember.member_type.asc(), GroupMember.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []

    # Bucket ids by member_type, then batch-fetch display fields.
    by_type: dict[str, set[uuid.UUID]] = {}
    for row in rows:
        by_type.setdefault(row.member_type, set()).add(row.member_id)

    display: dict[tuple[str, uuid.UUID], tuple[str, Optional[str]]] = {}

    if "user" in by_type:
        stmt = select(User.id, User.display_name, User.email).where(
            User.id.in_(by_type["user"]),
            User.organization_id == organization_id,
        )
        for uid, name, email in (await session.execute(stmt)).all():
            display[("user", uid)] = (name, email)

    if "organization_unit" in by_type:
        # LEFT JOIN with a per-unit member count so the subtitle can say
        # "department · 21 members" without a separate N+1 fetch.
        member_count_subq = (
            select(
                UserOrganizationUnit.organization_unit_id.label("ou_id"),
                func.count().label("cnt"),
            )
            .where(
                UserOrganizationUnit.organization_unit_id.in_(
                    by_type["organization_unit"]
                )
            )
            .group_by(UserOrganizationUnit.organization_unit_id)
            .subquery()
        )
        stmt = (
            select(
                OrganizationUnit.id,
                OrganizationUnit.name,
                OrganizationUnit.type,
                func.coalesce(member_count_subq.c.cnt, 0),
            )
            .join(
                member_count_subq,
                member_count_subq.c.ou_id == OrganizationUnit.id,
                isouter=True,
            )
            .where(
                OrganizationUnit.id.in_(by_type["organization_unit"]),
                OrganizationUnit.organization_id == organization_id,
            )
        )
        for oid, name, ou_type, cnt in (await session.execute(stmt)).all():
            # Subtitle is intentionally rendered in plain ASCII on the server —
            # localisation happens in the UI (i18n interpolation), so we
            # stash the numeric bits into a machine-parseable form.
            subtitle = f"{ou_type} · {cnt}"
            display[("organization_unit", oid)] = (name, subtitle)

    if "position" in by_type:
        occ_subq = (
            select(
                UserPosition.position_id.label("pid"),
                func.count().label("cnt"),
            )
            .where(UserPosition.position_id.in_(by_type["position"]))
            .where(UserPosition.end_date.is_(None))
            .where(UserPosition.organization_id == organization_id)
            .group_by(UserPosition.position_id)
            .subquery()
        )
        stmt = (
            select(
                Position.id,
                Position.name,
                Position.code,
                Position.grade,
                Position.headcount_budget,
                func.coalesce(occ_subq.c.cnt, 0),
            )
            .join(occ_subq, occ_subq.c.pid == Position.id, isouter=True)
            .where(
                Position.id.in_(by_type["position"]),
                Position.organization_id == organization_id,
            )
        )
        for pid, name, code, grade, budget, cnt in (
            await session.execute(stmt)
        ).all():
            # Subtitle for a position: "grade or code · N / budget". If both
            # grade and code are absent we degrade gracefully.
            prefix = grade or code or ""
            budget_str = str(budget) if budget else "∞"
            subtitle = f"{prefix} · {cnt}/{budget_str}".lstrip(" ·")
            display[("position", pid)] = (name, subtitle)

    if "service_account" in by_type:
        stmt = select(ServiceAccount.id, ServiceAccount.name).where(
            ServiceAccount.id.in_(by_type["service_account"]),
            ServiceAccount.organization_id == organization_id,
        )
        for uid, name in (await session.execute(stmt)).all():
            display[("service_account", uid)] = (name, None)

    views: list[GroupMemberView] = []
    for row in rows:
        key = (row.member_type, row.member_id)
        name, subtitle = display.get(key, ("(missing)", None))
        views.append(
            GroupMemberView(
                member=row,
                member_type=row.member_type,
                member_id=row.member_id,
                display_name=name,
                subtitle=subtitle,
            )
        )
    return views


async def list_groups_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    include_archived: bool = False,
) -> list[Group]:
    """Groups the user belongs to via any literal membership path.

    Paths considered (union):
      1. Direct: ``group_members.member_type='user'`` and matching user_id.
      2. Via org unit: user assigned to unit U; group contains U.
      3. Via position: user has a current tenure in position P; group
         contains P.
    """

    direct = (
        select(GroupMember.group_id)
        .where(GroupMember.member_type == "user")
        .where(GroupMember.member_id == user_id)
    )
    via_unit = (
        select(GroupMember.group_id)
        .join(
            UserOrganizationUnit,
            UserOrganizationUnit.organization_unit_id == GroupMember.member_id,
        )
        .where(GroupMember.member_type == "organization_unit")
        .where(UserOrganizationUnit.user_id == user_id)
        .where(UserOrganizationUnit.organization_id == organization_id)
    )
    via_position = (
        select(GroupMember.group_id)
        .join(
            UserPosition,
            UserPosition.position_id == GroupMember.member_id,
        )
        .where(GroupMember.member_type == "position")
        .where(UserPosition.user_id == user_id)
        .where(UserPosition.organization_id == organization_id)
        .where(UserPosition.end_date.is_(None))
    )
    union_subq = direct.union(via_unit, via_position).subquery()

    stmt = (
        select(Group)
        .where(Group.organization_id == organization_id)
        .where(Group.id.in_(select(union_subq.c.group_id)))
        .order_by(Group.name.asc())
    )
    if not include_archived:
        stmt = stmt.where(Group.status == "active")
    return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Mutations — definition
# ---------------------------------------------------------------------------


async def create_group(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    name: str,
    type: str = "static",
    description: Optional[str] = None,
    attributes: Optional[dict] = None,
) -> GroupView:
    validated_name = _validate_name(name)
    validated_type = _validate_type(type)

    async with session.begin():
        group = Group(
            organization_id=organization_id,
            type=validated_type,
            name=validated_name,
            description=description or None,
            status="active",
            attributes=attributes or {},
            sync_state="local",
        )
        session.add(group)
        try:
            await session.flush()
        except Exception as exc:  # noqa: BLE001
            raise AlreadyExistsError(
                f"group with name {validated_name!r} already exists in this organisation"
            ) from exc

        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="group.created",
                resource_type="group",
                resource_id=group.id,
                payload={"name": validated_name, "type": validated_type},
            )
        )
        await session.flush()
        await session.refresh(group, attribute_names=["created_at", "updated_at"])

    return GroupView(group=group, member_count=0)


async def update_group(
    session: AsyncSession,
    *,
    group_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    name: Optional[str] = None,
    type: Optional[str] = None,
    description: Optional[str] = None,
    attributes: Optional[dict] = None,
) -> GroupView:
    async with session.begin():
        group = await _load_group(
            session, group_id=group_id, organization_id=organization_id
        )
        if group.sync_state == "synced":
            raise InvalidInputError(
                "group is mirrored from an external directory and cannot be edited here"
            )

        changes: dict[str, object] = {}
        if name is not None:
            changes["name"] = _validate_name(name)
        if type is not None:
            changes["type"] = _validate_type(type)
        if description is not None:
            changes["description"] = description or None
        if attributes is not None:
            changes["attributes"] = attributes

        if not changes:
            count = await _member_count(session, group_id=group.id)
            return GroupView(group=group, member_count=count)

        for field, value in changes.items():
            setattr(group, field, value)
        try:
            await session.flush()
        except Exception as exc:  # noqa: BLE001
            raise AlreadyExistsError(
                "group name conflicts with another group in this organisation"
            ) from exc

        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="group.updated",
                resource_type="group",
                resource_id=group.id,
                payload={"changes": {k: str(v) for k, v in changes.items()}},
            )
        )
        await session.flush()
        await session.refresh(group, attribute_names=["updated_at"])

    return GroupView(
        group=group,
        member_count=await _member_count(session, group_id=group.id),
    )


async def archive_group(
    session: AsyncSession,
    *,
    group_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> GroupView:
    async with session.begin():
        group = await _load_group(
            session, group_id=group_id, organization_id=organization_id
        )
        if group.sync_state == "synced":
            raise InvalidInputError(
                "group is mirrored from an external directory; archive it there"
            )
        if group.status == "archived":
            count = await _member_count(session, group_id=group.id)
            return GroupView(group=group, member_count=count)

        # Detach members so counts stay honest; members are recreated if the
        # group is un-archived (out of scope for MVP).
        detach_stmt = select(GroupMember).where(GroupMember.group_id == group.id)
        detached = list((await session.execute(detach_stmt)).scalars().all())
        for member in detached:
            await session.delete(member)

        group.status = "archived"
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="group.archived",
                resource_type="group",
                resource_id=group.id,
                payload={"detached_members": len(detached)},
            )
        )
        await session.flush()
        await session.refresh(group, attribute_names=["updated_at"])

    return GroupView(group=group, member_count=0)


# ---------------------------------------------------------------------------
# Mutations — membership
# ---------------------------------------------------------------------------


async def add_member(
    session: AsyncSession,
    *,
    group_id: uuid.UUID,
    member_type: str,
    member_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> GroupMemberView:
    validated_type = _validate_member_type(member_type)

    async with session.begin():
        group = await _load_group(
            session, group_id=group_id, organization_id=organization_id
        )
        if group.status != "active":
            raise InvalidInputError("cannot modify members of an archived group")
        if group.sync_state == "synced":
            raise InvalidInputError(
                "group is mirrored from an external directory and cannot be modified here"
            )

        display_name, subtitle = await _resolve_member_target(
            session,
            member_type=validated_type,
            member_id=member_id,
            organization_id=organization_id,
        )

        existing_stmt = select(GroupMember).where(
            GroupMember.group_id == group_id,
            GroupMember.member_type == validated_type,
            GroupMember.member_id == member_id,
        )
        existing = (await session.execute(existing_stmt)).scalar_one_or_none()
        if existing is not None:
            raise AlreadyExistsError("this member is already in the group")

        link = GroupMember(
            group_id=group_id,
            member_type=validated_type,
            member_id=member_id,
            added_by_user_id=actor_user_id,
        )
        session.add(link)

        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="group.member_added",
                resource_type="group",
                resource_id=group_id,
                payload={
                    "member_type": validated_type,
                    "member_id": str(member_id),
                    "display_name": display_name,
                },
            )
        )
        await session.flush()
        await session.refresh(link, attribute_names=["created_at", "updated_at"])

    return GroupMemberView(
        member=link,
        member_type=validated_type,
        member_id=member_id,
        display_name=display_name,
        subtitle=subtitle,
    )


async def remove_member(
    session: AsyncSession,
    *,
    group_id: uuid.UUID,
    member_type: str,
    member_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> None:
    validated_type = _validate_member_type(member_type)

    async with session.begin():
        group = await _load_group(
            session, group_id=group_id, organization_id=organization_id
        )
        if group.sync_state == "synced":
            raise InvalidInputError(
                "group is mirrored from an external directory; edit membership there"
            )

        stmt = select(GroupMember).where(
            GroupMember.group_id == group_id,
            GroupMember.member_type == validated_type,
            GroupMember.member_id == member_id,
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
                action="group.member_removed",
                resource_type="group",
                resource_id=group_id,
                payload={
                    "member_type": validated_type,
                    "member_id": str(member_id),
                },
            )
        )


# ---------------------------------------------------------------------------
# Effective-members reverse expansion
# ---------------------------------------------------------------------------

# The set of hit paths a *group* can produce is a strict subset of the ones
# used by ``authz.list_effective_space_members`` — no role_binding is
# involved, and group membership is literal (no org-unit ancestor walk).
#
# Kept as a plain string so JSON serialisation can pass through untouched.
GroupHitPath = str  # "direct" | "via_position" | "via_organization_unit"


@dataclass(frozen=True)
class EffectiveGroupHitPath:
    """One reason a user is inside a group."""

    hit_path: GroupHitPath
    subject_type: str  # "user" | "position" | "organization_unit"
    subject_id: uuid.UUID
    subject_display_name: str


@dataclass(frozen=True)
class EffectiveGroupMemberView:
    """A single user's effective membership in a group, plus all paths."""

    user_id: uuid.UUID
    display_name: str
    email: str
    hit_paths: tuple[EffectiveGroupHitPath, ...]


async def list_effective_group_members(
    session: AsyncSession,
    *,
    group_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> list[EffectiveGroupMemberView]:
    """Every real user this group contains, deduped, with reasons.

    Rules mirror the (documented) group-membership semantics:

      * ``member_type='user'``               → hit_path ``direct``
      * ``member_type='position'``           → current occupants,
                                               hit_path ``via_position``
      * ``member_type='organization_unit'``  → users directly assigned to
                                               the unit (no descendant walk),
                                               hit_path ``via_organization_unit``
      * ``member_type='service_account'``    → skipped (not a human)

    A user hit by multiple paths keeps all of them; the caller can decide
    how to render.
    """

    # Ensure the group exists in this org (raises NotFoundError otherwise).
    await _load_group(session, group_id=group_id, organization_id=organization_id)

    rows = list(
        (
            await session.execute(
                select(GroupMember).where(GroupMember.group_id == group_id)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []

    # Bucket by member type for batched lookups.
    user_ids: set[uuid.UUID] = set()
    position_ids: set[uuid.UUID] = set()
    unit_ids: set[uuid.UUID] = set()
    for r in rows:
        if r.member_type == "user":
            user_ids.add(r.member_id)
        elif r.member_type == "position":
            position_ids.add(r.member_id)
        elif r.member_type == "organization_unit":
            unit_ids.add(r.member_id)

    # Batched display-name lookups so the hit-path renderer can show what
    # the user matched *through* (position name / unit name).
    position_names: dict[uuid.UUID, str] = {}
    if position_ids:
        rows_p = (
            await session.execute(
                select(Position.id, Position.name).where(Position.id.in_(position_ids))
            )
        ).all()
        position_names = {pid: name for pid, name in rows_p}

    unit_names: dict[uuid.UUID, str] = {}
    if unit_ids:
        rows_u = (
            await session.execute(
                select(OrganizationUnit.id, OrganizationUnit.name).where(
                    OrganizationUnit.id.in_(unit_ids)
                )
            )
        ).all()
        unit_names = {oid: name for oid, name in rows_u}

    # ``user_paths[user_id] = [hit_path, ...]``
    user_paths: dict[uuid.UUID, list[EffectiveGroupHitPath]] = {}

    def _record(uid: uuid.UUID, path: EffectiveGroupHitPath) -> None:
        user_paths.setdefault(uid, []).append(path)

    # Direct user members: the user *is* the subject.
    for uid in user_ids:
        _record(
            uid,
            EffectiveGroupHitPath(
                hit_path="direct",
                subject_type="user",
                subject_id=uid,
                subject_display_name="",
            ),
        )

    # Position members → occupants (open tenures).
    if position_ids:
        occ_rows = (
            await session.execute(
                select(UserPosition.position_id, UserPosition.user_id)
                .where(UserPosition.position_id.in_(position_ids))
                .where(UserPosition.organization_id == organization_id)
                .where(UserPosition.end_date.is_(None))
            )
        ).all()
        for pid, uid in occ_rows:
            _record(
                uid,
                EffectiveGroupHitPath(
                    hit_path="via_position",
                    subject_type="position",
                    subject_id=pid,
                    subject_display_name=position_names.get(pid, ""),
                ),
            )

    # Org-unit members → users directly in the unit (no ancestor/descendant walk).
    if unit_ids:
        uom_rows = (
            await session.execute(
                select(
                    UserOrganizationUnit.organization_unit_id,
                    UserOrganizationUnit.user_id,
                ).where(UserOrganizationUnit.organization_unit_id.in_(unit_ids))
            )
        ).all()
        for oid, uid in uom_rows:
            _record(
                uid,
                EffectiveGroupHitPath(
                    hit_path="via_organization_unit",
                    subject_type="organization_unit",
                    subject_id=oid,
                    subject_display_name=unit_names.get(oid, ""),
                ),
            )

    if not user_paths:
        return []

    all_uids = list(user_paths.keys())
    urows = (
        await session.execute(
            select(User.id, User.display_name, User.email).where(
                User.id.in_(all_uids),
                User.organization_id == organization_id,
            )
        )
    ).all()
    by_uid: dict[uuid.UUID, tuple[str, str]] = {
        uid: (name, email) for uid, name, email in urows
    }

    result: list[EffectiveGroupMemberView] = []
    for uid, paths in user_paths.items():
        meta = by_uid.get(uid)
        if meta is None:
            continue
        display_name, email = meta
        result.append(
            EffectiveGroupMemberView(
                user_id=uid,
                display_name=display_name,
                email=email,
                hit_paths=tuple(paths),
            )
        )

    result.sort(key=lambda v: v.display_name.lower())
    return result
