"""Space model.

A ``Space`` is the primary tenancy-of-work unit *inside* an ``Organization``.
Knowledge, agents, workflows, and data connections are anchored to a space so
that governance (visibility, trust level, retention, model policy) can be set
in exactly one place.

Design notes:

* ``preset`` picks a governance template — ``personal`` / ``collaborative`` /
  ``managed``. These are only *defaults*: any concrete field on the row
  (visibility, trust_level, ...) can be overridden.
* ``owner_type`` + ``owner_id`` is a *polymorphic* pointer. We deliberately do
  not add a FK because owners can be users, groups, org units, or the whole
  organisation. A check constraint keeps ``owner_type`` valid; service-layer
  code enforces referential integrity.
* Spaces can nest via ``parent_space_id`` so an org can express a project ↦
  sub-project relationship without turning ``OrganizationUnit`` into a
  workspace itself.
* ``inheritance_policy`` reserves room for later governance work (which fields
  cascade from parent, which are overridden). Empty ``{}`` today.
"""

from __future__ import annotations

import uuid
from typing import Optional

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from quberi_api.db.base import Base
from quberi_api.db.types import TimestampMixin, UUIDPk
from quberi_api.domain._constraints import in_values
from quberi_api.domain.enums import (
    SPACE_ACCESS_REQUEST_ROLES,
    SPACE_ACCESS_REQUEST_STATUSES,
    SPACE_OWNER_TYPES,
    SPACE_PRESETS,
    SPACE_STATUS,
    SPACE_TRUST_LEVELS,
    SPACE_VISIBILITY,
)


class Space(TimestampMixin, Base):
    __tablename__ = "spaces"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "slug", name="uq_spaces_organization_id_slug"
        ),
        in_values("preset", SPACE_PRESETS, name="preset"),
        in_values("owner_type", SPACE_OWNER_TYPES, name="owner_type"),
        in_values("visibility", SPACE_VISIBILITY, name="visibility"),
        in_values("trust_level", SPACE_TRUST_LEVELS, name="trust_level"),
        in_values("status", SPACE_STATUS, name="status"),
        Index("ix_spaces_organization_id", "organization_id"),
        Index("ix_spaces_owner", "owner_type", "owner_id"),
        Index("ix_spaces_parent_space_id", "parent_space_id"),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_space_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("spaces.id", ondelete="SET NULL"),
        nullable=True,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)

    preset: Mapped[str] = mapped_column(String(32), nullable=False, default="collaborative")
    owner_type: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="private")
    trust_level: Mapped[str] = mapped_column(String(32), nullable=False, default="guided")

    inheritance_policy: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    parent: Mapped[Optional[Space]] = relationship(
        remote_side="Space.id",
        back_populates="children",
    )
    children: Mapped[list[Space]] = relationship(back_populates="parent")


class SpaceAccessRequest(TimestampMixin, Base):
    """An administrator's request for access to a space, decided by the space's owner.

    An organisation administrator can manage an enterprise space without being able to read
    it, and nobody can give themselves access, so the way in is to ask. The request names
    the role wanted and why; an available owner of the space approves (which grants the
    role) or rejects it. One pending request per person and space.
    """

    __tablename__ = "space_access_requests"
    __table_args__ = (
        in_values("status", SPACE_ACCESS_REQUEST_STATUSES, name="status"),
        in_values("role_code", SPACE_ACCESS_REQUEST_ROLES, name="role_code"),
        Index(
            "uq_space_access_requests_pending",
            "space_id",
            "requester_user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_space_access_requests_space", "space_id", "status"),
        Index("ix_space_access_requests_requester", "requester_user_id", "status"),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False
    )
    requester_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_code: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    decided_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decision_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
