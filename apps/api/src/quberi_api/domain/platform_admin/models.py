"""Platform operator accounts.

A platform admin is the operator of a deployment (not a member of any
organisation): they open ``/admin`` to look after organisations, platform
models and usage. This is deliberately separate from ``users``/``identities``
so that nothing an organisation owner can do reaches it.

Only a salted, slow hash of the password is stored. ``token_version`` is part
of every signed admin session; raising it (a password change) cuts off every
session issued before.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from quberi_api.db.base import Base
from quberi_api.db.types import TimestampMixin, UUIDPk


class PlatformAdmin(TimestampMixin, Base):
    __tablename__ = "platform_admins"
    __table_args__ = (
        UniqueConstraint("username", name="uq_platform_admins_username"),
    )

    id: Mapped[UUIDPk]
    # Stored lower-case; compared case-insensitively.
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # True for a password the operator did not choose (generated at first
    # boot): the account can only change its password until it does.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
