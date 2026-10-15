"""Integration ORM models.

See ``alembic/versions/0011_integrations.py`` for the rationale. In
short:

* :class:`Integration` — one (org, provider) connection.
* :class:`IntegrationUserToken` — per-user OAuth grant (encrypted
  access + refresh tokens).
* :class:`IntegrationOAuthState` — short-lived CSRF nonce for the
  authorize→callback round trip.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from quberi_api.db.base import Base
from quberi_api.db.types import TimestampMixin, UUIDPk


class Integration(TimestampMixin, Base):
    """A per-organisation connection to an external provider."""

    __tablename__ = "integrations"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('feishu', 'dingtalk', 'wecom')",
            name="ck_integrations_provider",
        ),
        CheckConstraint(
            "status IN ('active', 'paused', 'error', 'archived')",
            name="ck_integrations_status",
        ),
        UniqueConstraint(
            "organization_id",
            "provider",
            name="uq_integrations_organization_provider",
        ),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active"
    )
    # Fernet-sealed {app_id, app_secret}; NULL → platform default app.
    app_credentials_encrypted: Mapped[Optional[str]] = mapped_column(
        String(4096), nullable=True
    )
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class IntegrationUserToken(TimestampMixin, Base):
    """One user's OAuth grant against an :class:`Integration`."""

    __tablename__ = "integration_user_tokens"
    __table_args__ = (
        UniqueConstraint(
            "integration_id",
            "user_id",
            name="uq_integration_user_tokens_integration_user",
        ),
        Index("ix_integration_user_tokens_user_id", "user_id"),
    )

    id: Mapped[UUIDPk]
    integration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integrations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    external_user_id: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True
    )
    access_token_encrypted: Mapped[str] = mapped_column(
        String(8192), nullable=False
    )
    refresh_token_encrypted: Mapped[Optional[str]] = mapped_column(
        String(8192), nullable=True
    )
    scopes: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refresh_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class IntegrationOAuthState(Base):
    """Single-use CSRF nonce for the OAuth authorize→callback round trip.

    Not a ``TimestampMixin`` — it carries its own ``created_at`` +
    ``expires_at`` and is deleted on use.
    """

    __tablename__ = "integration_oauth_states"
    __table_args__ = (
        UniqueConstraint("state", name="uq_integration_oauth_states_state"),
        Index("ix_integration_oauth_states_expires_at", "expires_at"),
    )

    id: Mapped[UUIDPk]
    state: Mapped[str] = mapped_column(String(128), nullable=False)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    redirect_after: Mapped[Optional[str]] = mapped_column(
        String(1024), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
