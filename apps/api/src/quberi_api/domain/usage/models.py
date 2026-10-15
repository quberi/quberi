"""Free-quota usage metering.

Tracks how many chat messages an organisation has spent against the
*platform-shared* model in the current period. Only usage that draws on
Quberi's shared key is counted — BYOK orgs (their own key, their own
cost) are never metered.

One row per ``(organization_id, period_start)``. ``period_start`` is the
first day of the month (UTC); a new month naturally gets a new row, which
is how the monthly reset works without a scheduled job.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from quberi_api.db.base import Base
from quberi_api.db.types import TimestampMixin, UUIDPk


class UsageCounter(TimestampMixin, Base):
    """Per-org, per-period counter of platform-key chat messages."""

    __tablename__ = "usage_counters"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "period_start",
            name="uq_usage_counters_org_period",
        ),
        Index("ix_usage_counters_organization_id", "organization_id"),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # First day of the metering period (month), UTC. New month → new row.
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    chat_messages_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )


class GuestStarterStat(Base):
    """How often each suggested question was asked by guests, per day (UTC).

    Powers the "popular" ordering and badge on the guest chat page. It holds
    only a curated suggestion key and a count - never any text a visitor
    typed, and no IP or account - so it is safe to keep and aggregate.
    """

    __tablename__ = "guest_starter_stats"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    starter_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
