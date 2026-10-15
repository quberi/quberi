"""Agent access-grant service.

Layer above ``RoleBinding`` for ``resource_type='agent'``. A grant means
"this subject may see + run this agent". We reuse the platform's
role-binding machinery wholesale: the grant is a ``RoleBinding`` with the
system ``viewer`` role on the agent resource, and visibility checks go
through the standard ``resolve_effective_role_codes`` evaluator (so a grant
to a department/position/group fans out to its members automatically).

Only meaningful when the agent's ``visibility='restricted'``; for an
``organization`` agent everyone already has access and grants are ignored.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.authz.models import Role, RoleBinding
from quberi_api.domain.identity.models import User
from quberi_api.domain.org.models import Group, OrganizationUnit
from quberi_api.domain.positions.models import Position
from quberi_api.services.errors import (
    AlreadyExistsError,
    InvalidInputError,
    NotFoundError,
)

# The grant role. "viewer" reads naturally as "can view/use this agent" and
# is a seeded system role, so no new role type is needed.
_GRANT_ROLE_CODE = "viewer"

_SUBJECT_TYPES: frozenset[str] = frozenset(
    {"user", "group", "organization_unit", "position"}
)


@dataclass(frozen=True)
class AgentAccessView:
    binding: RoleBinding
    subject_type: str
    subject_id: uuid.UUID
    subject_display_name: str
    subject_subtitle: Optional[str] = None


def _validate_subject_type(subject_type: str) -> str:
    if subject_type not in _SUBJECT_TYPES:
        raise InvalidInputError(
            f"unknown subject_type: {subject_type!r} "
            f"(allowed: {sorted(_SUBJECT_TYPES)})"
        )
    return subject_type


async def _resolve_subject_display(
    session: AsyncSession,
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> tuple[str, Optional[str]]:
    """Return ``(display_name, subtitle)`` for the subject; 404 if absent."""

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
    raise InvalidInputError(f"unknown subject_type: {subject_type!r}")


async def _grant_role(session: AsyncSession) -> Role:
    role = (
        await session.execute(
            select(Role).where(
                Role.code == _GRANT_ROLE_CODE,
                Role.is_system.is_(True),
                Role.organization_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    if role is None:
        raise NotFoundError(f"system role {_GRANT_ROLE_CODE!r} not found")
    return role


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_agent_access(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> list[AgentAccessView]:
    rows = list(
        (
            await session.execute(
                select(RoleBinding)
                .where(RoleBinding.organization_id == organization_id)
                .where(RoleBinding.resource_type == "agent")
                .where(RoleBinding.resource_id == agent_id)
                .order_by(RoleBinding.created_at.asc())
            )
        ).scalars().all()
    )
    views: list[AgentAccessView] = []
    for binding in rows:
        try:
            name, subtitle = await _resolve_subject_display(
                session,
                subject_type=binding.subject_type,
                subject_id=binding.subject_id,
                organization_id=organization_id,
            )
        except NotFoundError:
            name, subtitle = "(missing)", None
        views.append(
            AgentAccessView(
                binding=binding,
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


async def grant_agent_access(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    agent_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    subject_type: str,
    subject_id: uuid.UUID,
) -> AgentAccessView:
    validated_type = _validate_subject_type(subject_type)

    async with session.begin():
        display_name, subtitle = await _resolve_subject_display(
            session,
            subject_type=validated_type,
            subject_id=subject_id,
            organization_id=organization_id,
        )
        role = await _grant_role(session)

        existing = (
            await session.execute(
                select(RoleBinding).where(
                    RoleBinding.organization_id == organization_id,
                    RoleBinding.role_id == role.id,
                    RoleBinding.subject_type == validated_type,
                    RoleBinding.subject_id == subject_id,
                    RoleBinding.resource_type == "agent",
                    RoleBinding.resource_id == agent_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise AlreadyExistsError("this subject already has access to the agent")

        binding = RoleBinding(
            organization_id=organization_id,
            role_id=role.id,
            subject_type=validated_type,
            subject_id=subject_id,
            resource_type="agent",
            resource_id=agent_id,
            conditions={},
            created_by_user_id=actor_user_id,
        )
        session.add(binding)
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="agent.access_granted",
                resource_type="agent",
                resource_id=agent_id,
                payload={
                    "subject_type": validated_type,
                    "subject_id": str(subject_id),
                    "display_name": display_name,
                },
            )
        )
        await session.flush()
        await session.refresh(binding, attribute_names=["created_at", "updated_at"])

    return AgentAccessView(
        binding=binding,
        subject_type=validated_type,
        subject_id=subject_id,
        subject_display_name=display_name,
        subject_subtitle=subtitle,
    )


async def revoke_agent_access(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    agent_id: uuid.UUID,
    binding_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> None:
    async with session.begin():
        binding = (
            await session.execute(
                select(RoleBinding).where(
                    RoleBinding.id == binding_id,
                    RoleBinding.organization_id == organization_id,
                    RoleBinding.resource_type == "agent",
                    RoleBinding.resource_id == agent_id,
                )
            )
        ).scalar_one_or_none()
        if binding is None:
            raise NotFoundError(f"agent access binding not found: {binding_id}")
        await session.delete(binding)
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="agent.access_revoked",
                resource_type="agent",
                resource_id=agent_id,
                payload={
                    "binding_id": str(binding_id),
                    "subject_type": binding.subject_type,
                    "subject_id": str(binding.subject_id),
                },
            )
        )
