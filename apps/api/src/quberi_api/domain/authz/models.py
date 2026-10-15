"""Authorization models: Role → Permission and RoleBinding.

Model layout
------------
* ``Permission`` — a *global* atomic capability (e.g. ``space:read``,
  ``space:manage``, ``org:admin``). No tenant scoping — permissions are the
  same language across the platform.
* ``Role`` — a named bundle of permissions. Can be *system* (org-agnostic
  built-ins such as ``owner``, ``editor``, ``viewer``) or *custom* (defined by
  a single organisation).
* ``RolePermission`` — join table between roles and permissions.
* ``RoleBinding`` — the *actual* grant: a ``subject`` (user / group / org unit
  / service account) holds ``role`` on a ``resource`` (organization / space).
  ``conditions`` is JSONB reserved for the ABAC DSL that lands in a later
  slice; it is not evaluated today.

Both ``subject`` and ``resource`` are polymorphic (type + id). We keep them
un-FK'd on the database side and enforce integrity in the service layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from quberi_api.db.base import Base
from quberi_api.db.types import TimestampMixin, UUIDPk
from quberi_api.domain._constraints import in_values
from quberi_api.domain.enums import ROLE_RESOURCE_TYPES, ROLE_SUBJECT_TYPES


class Permission(TimestampMixin, Base):
    __tablename__ = "permissions"
    __table_args__ = (
        UniqueConstraint("code", name="uq_permissions_code"),
    )

    id: Mapped[UUIDPk]
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)


class Role(TimestampMixin, Base):
    __tablename__ = "roles"
    __table_args__ = (
        # ``organization_id`` NULL for system roles; unique code within each
        # organisation (and globally for system roles).
        UniqueConstraint(
            "organization_id", "code", name="uq_roles_organization_id_code"
        ),
        Index("ix_roles_organization_id", "organization_id"),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    is_system: Mapped[bool] = mapped_column(nullable=False, default=False)

    permissions: Mapped[list[RolePermission]] = relationship(
        back_populates="role",
        cascade="all, delete-orphan",
    )


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    )

    role: Mapped[Role] = relationship(back_populates="permissions")
    permission: Mapped[Permission] = relationship()


class RoleBinding(TimestampMixin, Base):
    """A concrete grant of a role to a subject on a resource."""

    __tablename__ = "role_bindings"
    __table_args__ = (
        UniqueConstraint(
            "role_id",
            "subject_type",
            "subject_id",
            "resource_type",
            "resource_id",
            # Postgres identifier limit is 63 chars; the natural composite name
            # would blow that, so we shorten to ``_grant``.
            name="uq_role_bindings_grant",
        ),
        in_values("subject_type", ROLE_SUBJECT_TYPES, name="subject_type"),
        in_values("resource_type", ROLE_RESOURCE_TYPES, name="resource_type"),
        Index("ix_role_bindings_organization_id", "organization_id"),
        Index("ix_role_bindings_subject", "subject_type", "subject_id"),
        Index("ix_role_bindings_resource", "resource_type", "resource_id"),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
    )
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    conditions: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
