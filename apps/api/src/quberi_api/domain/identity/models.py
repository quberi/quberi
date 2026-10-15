"""Identity domain ORM models.

Model boundaries
----------------
* ``Organization`` — the tenant boundary. Every other domain table carries an
  ``organization_id`` FK referring back to this table.
* ``Identity`` — a *global* authentication identity (email/phone based). Not
  tenant-scoped so the same person can belong to multiple organisations under
  one login later on.
* ``ExternalIdentity`` — an SSO subject linked to an ``Identity``. Global.
* ``User`` — org-scoped membership of an ``Identity`` inside an
  ``Organization``. All tenant-scoped features hang off ``User.id``.
* ``ServiceAccount`` — org-scoped machine principal (used by CI, agents,
  scheduled jobs). Never linked to a human ``Identity``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, ForeignKey, Index, String, UniqueConstraint, false
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from quberi_api.db.base import Base
from quberi_api.db.types import TimestampMixin, UUIDPk
from quberi_api.domain._constraints import in_values
from quberi_api.domain._sync import DirectorySyncMixin
from quberi_api.domain.enums import (
    IDENTITY_PROVIDERS,
    IDENTITY_STATUS,
    ORGANIZATION_STATUS,
    ORGANIZATION_TIERS,
    SERVICE_ACCOUNT_STATUS,
    SYNC_STATES,
    USER_STATUS,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


class Organization(TimestampMixin, Base):
    """The tenant boundary. One row per customer organisation."""

    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_organizations_slug"),
        in_values("status", ORGANIZATION_STATUS, name="status"),
        in_values("tier", ORGANIZATION_TIERS, name="tier"),
        in_values(
            "platform_model_visibility",
            ("all", "none", "allowlist"),
            name="platform_model_visibility",
        ),
    )

    id: Mapped[UUIDPk]
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    # Product tier — ``team`` (default) keeps every existing org enterprise-shaped;
    # ``personal`` marks a self-serve single-member org whose UI hides org concepts.
    tier: Mapped[str] = mapped_column(String(16), nullable=False, default="team")
    default_locale: Mapped[str] = mapped_column(String(16), nullable=False, default="en-US")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    data_region: Mapped[str] = mapped_column(String(32), nullable=False, default="local")
    # Whether members may add their own private (BYOK) model bindings —
    # visible only to themselves, never to org admins. Admin-managed via the
    # admin console; defaults to allowed so existing orgs are unaffected.
    allow_byok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Whether members may connect outside tools (OAuth) that read this
    # organisation's knowledge. Off by default: those reads leave Quberi
    # without the "check before sending" step, so an owner/admin opts in.
    allow_external_tools: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # Platform-admin control over which platform-shared chat models (if
    # any) this org's members may even see in their model picker — some
    # customers don't want platform fallbacks exposed to users at all,
    # or only want a hand-picked subset. ``all`` (default) is today's
    # behaviour: every platform-default model is visible. ``none`` hides
    # every platform model (the org falls back to its own BYOK models
    # only). ``allowlist`` shows only the models listed in
    # ``OrganizationPlatformModelAllowlist`` for this org. This never
    # touches the org's own or members' private BYOK bindings — those
    # are always visible regardless of this setting.
    platform_model_visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="all", server_default="all"
    )
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class Identity(TimestampMixin, Base):
    """Global authentication identity (email + optional phone)."""

    __tablename__ = "identities"
    __table_args__ = (
        UniqueConstraint("primary_email", name="uq_identities_primary_email"),
        in_values("status", IDENTITY_STATUS, name="status"),
    )

    id: Mapped[UUIDPk]
    primary_email: Mapped[str] = mapped_column(String(320), nullable=False)
    primary_email_verified: Mapped[bool] = mapped_column(nullable=False, default=False)
    primary_phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    external_identities: Mapped[list[ExternalIdentity]] = relationship(
        back_populates="identity",
        cascade="all, delete-orphan",
    )


class ExternalIdentity(TimestampMixin, Base):
    """SSO subject linked to a global ``Identity``."""

    __tablename__ = "external_identities"
    __table_args__ = (
        UniqueConstraint(
            "provider", "subject", name="uq_external_identities_provider_subject"
        ),
        in_values("provider", IDENTITY_PROVIDERS, name="provider"),
    )

    id: Mapped[UUIDPk]
    identity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("identities.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    identity: Mapped[Identity] = relationship(back_populates="external_identities")


class User(DirectorySyncMixin, TimestampMixin, Base):
    """Org-scoped membership of an ``Identity`` inside an ``Organization``."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "identity_id", name="uq_users_organization_id_identity_id"
        ),
        UniqueConstraint("organization_id", "email", name="uq_users_organization_id_email"),
        UniqueConstraint(
            "external_provider_id",
            "external_id",
            name="uq_users_external",
        ),
        in_values("status", USER_STATUS, name="status"),
        in_values("sync_state", SYNC_STATES, name="sync_state"),
        Index("ix_users_organization_id", "organization_id"),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    identity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("identities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    avatar_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="en-US")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="invited")
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Populated by relationships defined in other modules once they import this
    # table. We only need the org side here to keep this file self-contained.


class ServiceAccount(TimestampMixin, Base):
    """Org-scoped machine principal (agents, integrations, scheduled jobs)."""

    __tablename__ = "service_accounts"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "name", name="uq_service_accounts_organization_id_name"
        ),
        in_values("status", SERVICE_ACCOUNT_STATUS, name="status"),
        Index("ix_service_accounts_organization_id", "organization_id"),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
