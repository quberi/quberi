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

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from quberi_api.db.base import Base
from quberi_api.db.types import TimestampMixin, UUIDPk
from quberi_api.domain._constraints import in_values
from quberi_api.domain.enums import (
    GENERIC_STATUS,
    SPACE_OWNER_TYPES,
    SPACE_PRESETS,
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
        in_values("status", GENERIC_STATUS, name="status"),
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
