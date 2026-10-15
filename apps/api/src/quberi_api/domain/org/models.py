"""Organisation graph models.

Two orthogonal structures live here:

* ``OrganizationUnit`` — the *hierarchy* (departments, teams, business units,
  legal entities ...). Arbitrary depth via a self-referential ``parent_id``.
  Type is left open (``department``/``team``/``business_unit``/...``custom``)
  so each customer can model their own structure without a schema change.
* ``Group`` / ``GroupMember`` — the *matrix*. Groups cut across the hierarchy
  (project teams, security cohorts, dynamic cohorts). Membership is polymorphic
  so a group can contain users, whole org units, or service accounts.
* ``UserOrganizationUnit`` — many-to-many linking users to org units. A user
  can belong to multiple units; at most one of those may be marked
  ``is_primary`` (enforced by a partial unique index in the migration).

Dynamic group evaluation (``type = 'dynamic'``) is intentionally out of scope
for this slice; the ``attributes`` JSONB is reserved for the rule DSL that
lands in a later slice.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from quberi_api.db.base import Base
from quberi_api.db.types import TimestampMixin, UUIDPk
from quberi_api.domain._constraints import in_values
from quberi_api.domain._sync import DirectorySyncMixin
from quberi_api.domain.enums import (
    GENERIC_STATUS,
    GROUP_MEMBER_TYPES,
    GROUP_TYPES,
    ORG_UNIT_TYPES,
    SYNC_STATES,
)


class OrganizationUnit(DirectorySyncMixin, TimestampMixin, Base):
    """A node in the organisation hierarchy (arbitrary depth)."""

    __tablename__ = "organization_units"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "parent_id",
            "code",
            name="uq_organization_units_organization_id_parent_id_code",
        ),
        UniqueConstraint(
            "external_provider_id",
            "external_id",
            name="uq_organization_units_external",
        ),
        in_values("type", ORG_UNIT_TYPES, name="type"),
        in_values("status", GENERIC_STATUS, name="status"),
        in_values("sync_state", SYNC_STATES, name="sync_state"),
        Index("ix_organization_units_organization_id", "organization_id"),
        Index("ix_organization_units_parent_id", "parent_id"),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("organization_units.id", ondelete="CASCADE"),
        nullable=True,
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False, default="department")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    manager_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    parent: Mapped[Optional[OrganizationUnit]] = relationship(
        remote_side="OrganizationUnit.id",
        back_populates="children",
    )
    children: Mapped[list[OrganizationUnit]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
    )


class Group(DirectorySyncMixin, TimestampMixin, Base):
    """A cross-cutting cohort. Static, dynamic, security, or project."""

    __tablename__ = "groups"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "name", name="uq_groups_organization_id_name"
        ),
        UniqueConstraint(
            "external_provider_id",
            "external_id",
            name="uq_groups_external",
        ),
        in_values("type", GROUP_TYPES, name="type"),
        in_values("status", GENERIC_STATUS, name="status"),
        in_values("sync_state", SYNC_STATES, name="sync_state"),
        Index("ix_groups_organization_id", "organization_id"),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False, default="static")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    # ``attributes`` holds the dynamic-membership rule DSL (evaluated in a later
    # slice). Keeping it here rather than a sibling table avoids an extra join
    # on every membership recompute.
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    members: Mapped[list[GroupMember]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
    )


class GroupMember(TimestampMixin, Base):
    """Polymorphic membership row for a ``Group``.

    A member is identified by ``(member_type, member_id)`` where ``member_type``
    is one of ``user`` / ``organization_unit`` / ``service_account``. We do not
    add a database-level FK on ``member_id`` because it points at different
    tables depending on ``member_type``; integrity is enforced in the service
    layer plus a check constraint on ``member_type``.
    """

    __tablename__ = "group_members"
    __table_args__ = (
        UniqueConstraint(
            "group_id",
            "member_type",
            "member_id",
            name="uq_group_members_group_id_member_type_member_id",
        ),
        in_values("member_type", GROUP_MEMBER_TYPES, name="member_type"),
        Index("ix_group_members_group_id", "group_id"),
        Index("ix_group_members_member", "member_type", "member_id"),
    )

    id: Mapped[UUIDPk]
    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    member_type: Mapped[str] = mapped_column(String(32), nullable=False)
    member_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    added_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    group: Mapped[Group] = relationship(back_populates="members")


class UserOrganizationUnit(TimestampMixin, Base):
    """Association linking a ``User`` to zero or more ``OrganizationUnit``s.

    ``organization_id`` is denormalised so tenant-scoped queries can filter
    without a triple join. The partial unique index on ``(user_id) WHERE
    is_primary`` (installed in migration 0005) enforces at-most-one primary
    unit per user.
    """

    __tablename__ = "user_organization_units"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "organization_unit_id",
            name="uq_user_organization_units_user_id_organization_unit_id",
        ),
        # Partial unique index — at most one primary org unit per user.
        Index(
            "uq_user_organization_units_primary_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
        Index("ix_user_organization_units_organization_id", "organization_id"),
        Index("ix_user_organization_units_user_id", "user_id"),
        Index(
            "ix_user_organization_units_organization_unit_id",
            "organization_unit_id",
        ),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_unit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organization_units.id", ondelete="CASCADE"),
        nullable=False,
    )
    is_primary: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
    added_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
