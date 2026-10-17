"""Space access-binding service.

Layer above ``RoleBinding`` for the specific case of ``resource_type='space'``.
It handles subject validation, display-name resolution, and the write-guard
policy (space owner OR org admin/owner).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.authz.catalog import SYSTEM_ROLE_CODES
from quberi_api.domain.authz.models import Role, RoleBinding
from quberi_api.domain.identity.models import Organization, User
from quberi_api.domain.org.models import Group, OrganizationUnit
from quberi_api.domain.positions.models import Position
from quberi_api.domain.spaces.models import Space
from quberi_api.services import access_guard
from quberi_api.services.errors import (
    AlreadyExistsError,
    ForbiddenError,
    InvalidInputError,
    NotFoundError,
)

# ``owner`` is bootstrap-only for organisations; on a *space* we allow it as
# an intentional grant (which is exactly what create_collaborative_space
# writes for the creator). Editor/viewer/admin cover the day-to-day sharing
# gradient; member has no meaning at the space level so we exclude it.
_ASSIGNABLE_SPACE_ROLES: frozenset[str] = frozenset({"owner", "admin", "editor", "viewer"})

_SUBJECT_TYPES: frozenset[str] = frozenset(
    {"user", "group", "organization_unit", "position", "organization"}
)


@dataclass(frozen=True)
class AccessBindingView:
    binding: RoleBinding
    role_code: str
    subject_type: str
    subject_id: uuid.UUID
    subject_display_name: str
    subject_subtitle: Optional[str] = None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_role_code(role_code: str) -> str:
    if role_code not in SYSTEM_ROLE_CODES:
        raise InvalidInputError(f"unknown role: {role_code!r}")
    if role_code not in _ASSIGNABLE_SPACE_ROLES:
        raise InvalidInputError(
            f"role {role_code!r} is not assignable at the space level "
            f"(allowed: {sorted(_ASSIGNABLE_SPACE_ROLES)})"
        )
    return role_code


def _validate_subject_type(subject_type: str) -> str:
    if subject_type not in _SUBJECT_TYPES:
        raise InvalidInputError(
            f"unknown subject_type: {subject_type!r} "
            f"(allowed: {sorted(_SUBJECT_TYPES)})"
        )
    return subject_type


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


async def _load_space(
    session: AsyncSession, *, space_id: uuid.UUID, organization_id: uuid.UUID
) -> Space:
    stmt = select(Space).where(
        Space.id == space_id,
        Space.organization_id == organization_id,
    )
    space = (await session.execute(stmt)).scalar_one_or_none()
    if space is None:
        raise NotFoundError(f"space not found: {space_id}")
    return space


async def _resolve_subject_display(
    session: AsyncSession,
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> tuple[str, Optional[str]]:
    """Return ``(display_name, subtitle)`` for the referenced subject."""

    if subject_type == "user":
        row = (
            await session.execute(
                select(User.display_name, User.email).where(
                    User.id == subject_id,
                    User.organization_id == organization_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError(f"user not found: {subject_id}")
        return row[0], row[1]
    if subject_type == "group":
        row = (
            await session.execute(
                select(Group.name, Group.type).where(
                    Group.id == subject_id,
                    Group.organization_id == organization_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError(f"group not found: {subject_id}")
        return row[0], row[1]
    if subject_type == "organization_unit":
        row = (
            await session.execute(
                select(OrganizationUnit.name, OrganizationUnit.type).where(
                    OrganizationUnit.id == subject_id,
                    OrganizationUnit.organization_id == organization_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError(f"org unit not found: {subject_id}")
        return row[0], row[1]
    if subject_type == "position":
        row = (
            await session.execute(
                select(Position.name, Position.code).where(
                    Position.id == subject_id,
                    Position.organization_id == organization_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError(f"position not found: {subject_id}")
        return row[0], row[1]
    if subject_type == "organization":
        # "Everyone in the organisation" — the only valid subject is the
        # caller's own organisation, never another tenant's.
        if subject_id != organization_id:
            raise InvalidInputError(
                "an organization subject must be the current organization"
            )
        name = (
            await session.execute(
                select(Organization.name).where(Organization.id == organization_id)
            )
        ).scalar_one_or_none()
        if name is None:
            raise NotFoundError(f"organization not found: {organization_id}")
        return name, None
    raise InvalidInputError(f"unknown subject_type: {subject_type!r}")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_bindings(
    session: AsyncSession,
    *,
    space_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> list[AccessBindingView]:
    await _load_space(session, space_id=space_id, organization_id=organization_id)
    rows = list(
        (
            await session.execute(
                select(RoleBinding, Role.code)
                .join(Role, Role.id == RoleBinding.role_id)
                .where(RoleBinding.organization_id == organization_id)
                .where(RoleBinding.resource_type == "space")
                .where(RoleBinding.resource_id == space_id)
                .order_by(RoleBinding.created_at.asc())
            )
        ).all()
    )
    if not rows:
        return []

    # Batch subject-display lookup per type.
    by_type: dict[str, set[uuid.UUID]] = {}
    for binding, _ in rows:
        by_type.setdefault(binding.subject_type, set()).add(binding.subject_id)

    display: dict[tuple[str, uuid.UUID], tuple[str, Optional[str]]] = {}
    if "user" in by_type:
        for uid, name, email in (
            await session.execute(
                select(User.id, User.display_name, User.email).where(
                    User.id.in_(by_type["user"]),
                    User.organization_id == organization_id,
                )
            )
        ).all():
            display[("user", uid)] = (name, email)
    if "group" in by_type:
        for gid, name, gtype in (
            await session.execute(
                select(Group.id, Group.name, Group.type).where(
                    Group.id.in_(by_type["group"]),
                    Group.organization_id == organization_id,
                )
            )
        ).all():
            display[("group", gid)] = (name, gtype)
    if "organization_unit" in by_type:
        for oid, name, ou_type in (
            await session.execute(
                select(
                    OrganizationUnit.id,
                    OrganizationUnit.name,
                    OrganizationUnit.type,
                ).where(
                    OrganizationUnit.id.in_(by_type["organization_unit"]),
                    OrganizationUnit.organization_id == organization_id,
                )
            )
        ).all():
            display[("organization_unit", oid)] = (name, ou_type)
    if "position" in by_type:
        for pid, name, code in (
            await session.execute(
                select(Position.id, Position.name, Position.code).where(
                    Position.id.in_(by_type["position"]),
                    Position.organization_id == organization_id,
                )
            )
        ).all():
            display[("position", pid)] = (name, code)
    if "organization" in by_type:
        org_name = (
            await session.execute(
                select(Organization.name).where(Organization.id == organization_id)
            )
        ).scalar_one_or_none()
        if org_name is not None:
            display[("organization", organization_id)] = (org_name, None)

    views: list[AccessBindingView] = []
    for binding, role_code in rows:
        name, subtitle = display.get(
            (binding.subject_type, binding.subject_id),
            ("(missing)", None),
        )
        views.append(
            AccessBindingView(
                binding=binding,
                role_code=role_code,
                subject_type=binding.subject_type,
                subject_id=binding.subject_id,
                subject_display_name=name,
                subject_subtitle=subtitle,
            )
        )
    return views


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


async def grant_in_transaction(
    session: AsyncSession,
    *,
    space: Space,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    subject_type: str,
    subject_id: uuid.UUID,
    role_code: str,
) -> AccessBindingView:
    """Insert one access binding using the caller's already-open transaction.

    Validates the subject belongs to ``organization_id`` and the role is
    assignable, so it is safe to call with client-supplied ids. It never
    opens or commits a transaction: :func:`add_binding` wraps it for the
    single-grant endpoint, and space creation calls it in a loop so the
    space and all of its initial grants succeed or fail together.
    """

    validated_subject_type = _validate_subject_type(subject_type)
    validated_role = _validate_role_code(role_code)
    guard = await access_guard.watch(
        session, actor_user_id=actor_user_id, organization_id=organization_id
    )
    if (
        validated_subject_type == "user"
        and validated_role in ("owner", "admin")
        and await access_guard.user_is_restricted_owner(
            session, organization_id=organization_id, user_id=subject_id
        )
    ):
        raise ForbiddenError(access_guard.OWNER_CANNOT_MANAGE_SPACES_MESSAGE)
    display_name, subtitle = await _resolve_subject_display(
        session,
        subject_type=validated_subject_type,
        subject_id=subject_id,
        organization_id=organization_id,
    )

    role_stmt = select(Role).where(
        Role.code == validated_role,
        Role.is_system.is_(True),
        Role.organization_id.is_(None),
    )
    role = (await session.execute(role_stmt)).scalar_one_or_none()
    if role is None:
        raise NotFoundError(f"system role {validated_role!r} not found")

    existing_stmt = select(RoleBinding).where(
        RoleBinding.organization_id == organization_id,
        RoleBinding.role_id == role.id,
        RoleBinding.subject_type == validated_subject_type,
        RoleBinding.subject_id == subject_id,
        RoleBinding.resource_type == "space",
        RoleBinding.resource_id == space.id,
    )
    if (await session.execute(existing_stmt)).scalar_one_or_none() is not None:
        raise AlreadyExistsError("this subject already has that role on the space")

    binding = RoleBinding(
        organization_id=organization_id,
        role_id=role.id,
        subject_type=validated_subject_type,
        subject_id=subject_id,
        resource_type="space",
        resource_id=space.id,
        conditions={},
        created_by_user_id=actor_user_id,
    )
    session.add(binding)
    session.add(
        AuditEvent(
            organization_id=organization_id,
            actor_type="user",
            actor_id=actor_user_id,
            action="space.access_granted",
            resource_type="space",
            resource_id=space.id,
            space_id=space.id,
            payload={
                "subject_type": validated_subject_type,
                "subject_id": str(subject_id),
                "role": validated_role,
                "display_name": display_name,
            },
        )
    )
    await session.flush()
    await guard.check()
    await session.refresh(binding, attribute_names=["created_at", "updated_at"])
    return AccessBindingView(
        binding=binding,
        role_code=validated_role,
        subject_type=validated_subject_type,
        subject_id=subject_id,
        subject_display_name=display_name,
        subject_subtitle=subtitle,
    )


async def add_binding(
    session: AsyncSession,
    *,
    space_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    subject_type: str,
    subject_id: uuid.UUID,
    role_code: str,
) -> AccessBindingView:
    async with session.begin():
        space = await _load_space(
            session, space_id=space_id, organization_id=organization_id
        )
        return await grant_in_transaction(
            session,
            space=space,
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            subject_type=subject_type,
            subject_id=subject_id,
            role_code=role_code,
        )


async def remove_binding(
    session: AsyncSession,
    *,
    binding_id: uuid.UUID,
    space_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> None:
    async with session.begin():
        stmt = select(RoleBinding, Role.code).join(
            Role, Role.id == RoleBinding.role_id
        ).where(
            RoleBinding.id == binding_id,
            RoleBinding.organization_id == organization_id,
            RoleBinding.resource_type == "space",
            RoleBinding.resource_id == space_id,
        )
        row = (await session.execute(stmt)).first()
        if row is None:
            raise NotFoundError(f"access binding not found: {binding_id}")

        binding, role_code = row
        await session.delete(binding)

        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="space.access_revoked",
                resource_type="space",
                resource_id=space_id,
                space_id=space_id,
                payload={
                    "binding_id": str(binding_id),
                    "subject_type": binding.subject_type,
                    "subject_id": str(binding.subject_id),
                    "role": role_code,
                },
            )
        )


# Public name for other services that attach a person/group/unit/position to a rule
# (document-level access rules use the same subjects as space grants).
resolve_subject_display = _resolve_subject_display
