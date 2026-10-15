"""Auth domain models: platform email settings + verification codes.

* :class:`PlatformEmailSettings` — a *singleton* (one row) holding the SMTP
  config the platform uses to send verification codes / invites. The
  password is Fernet-sealed (never stored plaintext). Seeded from env on
  first boot, admin-editable afterwards.
* :class:`VerificationCode` — a short-lived one-time code sent to an email
  (later: phone) to prove ownership during login. Codes are stored hashed,
  expire quickly, and cap failed attempts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from quberi_api.db.base import Base
from quberi_api.db.types import TimestampMixin, UUIDPk


class PlatformEmailSettings(TimestampMixin, Base):
    """One SMTP provider the platform can send through.

    Multiple rows are allowed: the sender tries the ``enabled`` providers
    in ascending ``priority`` order until one delivers (failover). This
    lets us keep e.g. a domestic (163) primary and an overseas (Gmail)
    fallback configured at once.
    """

    __tablename__ = "platform_email_settings"

    id: Mapped[UUIDPk]
    # Human label for the provider (e.g. "163", "Gmail").
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    # Lower = tried first.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=465)
    username: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    # Fernet-sealed SMTP password / authorization code.
    password_encrypted: Mapped[Optional[str]] = mapped_column(
        String(4096), nullable=True
    )
    from_address: Mapped[str] = mapped_column(String(320), nullable=False)
    use_ssl: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class VerificationCode(Base):
    """A one-time verification code bound to an email + purpose.

    Not a ``TimestampMixin`` — it carries its own ``created_at`` +
    ``expires_at`` and is short-lived / disposable.
    """

    __tablename__ = "auth_verification_codes"
    __table_args__ = (
        Index("ix_auth_verification_codes_email", "email"),
        Index("ix_auth_verification_codes_expires_at", "expires_at"),
    )

    id: Mapped[UUIDPk]
    # Channel handle — an email today; phone numbers reuse this table later.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    # sha256 hex of the code (never store the code itself).
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="login")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
