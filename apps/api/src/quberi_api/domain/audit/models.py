"""Audit event log.

Append-only. Every governance-relevant action lands here so downstream
compliance tools (SIEM, retention, e-discovery) have a single source of truth.

We deliberately do *not* enforce FKs on ``actor_id`` / ``resource_id`` /
``space_id`` — an audit row must survive the deletion of the entity it refers
to. Tenant scoping via ``organization_id`` is enforced.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from quberi_api.db.base import Base
from quberi_api.db.types import UUIDPk
from quberi_api.domain._constraints import in_values
from quberi_api.domain.enums import AUDIT_ACTOR_TYPES


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        in_values("actor_type", AUDIT_ACTOR_TYPES, name="actor_type"),
        Index("ix_audit_events_organization_id_created_at", "organization_id", "created_at"),
        Index("ix_audit_events_action", "action"),
        Index("ix_audit_events_resource", "resource_type", "resource_id"),
        Index("ix_audit_events_request_id", "request_id"),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)
    space_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)
    request_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    ip_address: Mapped[Optional[str]] = mapped_column(INET, nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
