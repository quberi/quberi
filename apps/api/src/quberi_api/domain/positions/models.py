"""Positions (job roles) and their occupants.

Design
------
* ``Position`` is the *definition* of a job role — "AI 平台负责人",
  "Senior Software Engineer". It carries the reporting line
  (``reports_to_position_id``), the department it belongs to
  (``organization_unit_id``), a headcount budget (0 = unlimited), and a
  lifecycle status.
* ``UserPosition`` records that ``user`` currently or previously occupied
  ``position`` between ``start_date`` and ``end_date`` (NULL = current).
  A partial unique index (installed by the migration) enforces
  "one *current* primary position per user".
* A user may **occupy multiple positions concurrently** (兼任) and
  **positions may be re-filled after a tenure ends** (rehire / rotation).
* ``reports_to_position_id`` is a self-referential FK so we can walk
  reporting lines position-to-position. ``ondelete='SET NULL'`` is defensive
  — positions are archived, never hard-deleted, so this never fires in
  practice.

Position-level ``sync_state`` reserves the row for future HRIS mirroring
(飞书/企微/钉钉/Workday). ``UserPosition`` is a link table and does not
need directory identity of its own.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from quberi_api.db.base import Base
from quberi_api.db.types import TimestampMixin, UUIDPk
from quberi_api.domain._constraints import in_values
from quberi_api.domain._sync import DirectorySyncMixin
from quberi_api.domain.enums import POSITION_STATUS, SYNC_STATES


class Position(DirectorySyncMixin, TimestampMixin, Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "code",
            name="uq_positions_organization_id_code",
        ),
        UniqueConstraint(
            "external_provider_id",
            "external_id",
            name="uq_positions_external",
        ),
        in_values("status", POSITION_STATUS, name="status"),
        in_values("sync_state", SYNC_STATES, name="sync_state"),
        Index("ix_positions_organization_id", "organization_id"),
        Index("ix_positions_organization_unit_id", "organization_unit_id"),
        Index("ix_positions_reports_to", "reports_to_position_id"),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    organization_unit_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("organization_units.id", ondelete="SET NULL"),
        nullable=True,
    )
    reports_to_position_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("positions.id", ondelete="SET NULL"),
        nullable=True,
    )
    job_family: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    grade: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # ``0`` = unlimited (used for evergreen roles like "engineer" where
    # headcount is not tracked); positive values cap concurrent occupants.
    headcount_budget: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active"
    )
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    reports_to: Mapped[Optional[Position]] = relationship(
        remote_side="Position.id",
        foreign_keys="Position.reports_to_position_id",
    )


class UserPosition(TimestampMixin, Base):
    """Association: ``user`` occupies ``position`` between ``start_date`` and ``end_date``."""

    __tablename__ = "user_positions"
    __table_args__ = (
        # One *current* tenure per (user, position). A user rehired into the
        # same role later is fine because the previous row has end_date set.
        Index(
            "uq_user_positions_current_tenure",
            "user_id",
            "position_id",
            unique=True,
            postgresql_where=text("end_date IS NULL"),
        ),
        # At most one primary position at a time per user.
        Index(
            "uq_user_positions_primary_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_primary AND end_date IS NULL"),
        ),
        Index("ix_user_positions_organization_id", "organization_id"),
        Index("ix_user_positions_user_id", "user_id"),
        Index("ix_user_positions_position_id", "position_id"),
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
    position_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("positions.id", ondelete="CASCADE"),
        nullable=False,
    )
    is_primary: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    added_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Convenience relationship for eager loading in service reads.
    position: Mapped[Position] = relationship(foreign_keys=[position_id])


# Silences ``F401`` where callers only need ``datetime`` for annotations.
_ = datetime
