"""OAuth grants for outside tools.

One row is one person's authorisation of one tool, in one organisation. The
row holds only *hashes* of the access and refresh tokens, so a database leak
does not hand out working tokens. Tokens rotate in place: each refresh
replaces both hashes and keeps the previous refresh hash, so a refresh token
that is presented a second time (a sign it was copied) revokes the grant.

A grant is not an account: it acts as the user it was granted by, and every
request re-checks that the user, the organisation and the organisation's
"allow external tools" switch are still in good standing.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from quberi_api.db.base import Base
from quberi_api.db.types import TimestampMixin, UUIDPk


class OAuthGrant(TimestampMixin, Base):
    __tablename__ = "oauth_grants"
    __table_args__ = (
        UniqueConstraint("access_hash", name="uq_oauth_grants_access_hash"),
        UniqueConstraint("refresh_hash", name="uq_oauth_grants_refresh_hash"),
        Index("ix_oauth_grants_organization_id", "organization_id"),
        Index("ix_oauth_grants_user_id", "user_id"),
        Index("ix_oauth_grants_previous_refresh_hash", "previous_refresh_hash"),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Shown to the person in their list of connected tools.
    client_name: Mapped[str] = mapped_column(String(200), nullable=False)
    scope: Mapped[str] = mapped_column(String(200), nullable=False)
    # Which program is actually calling, as that program introduced itself (for an MCP
    # connection: the name and version in its ``initialize`` request). ``client_name`` is
    # the registered tool and is the same for every connection of that tool; this is what
    # tells two of them apart. Self-reported, so display only, never a decision.
    caller_label: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    access_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    access_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    refresh_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_refresh_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    refresh_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Hard ceiling: refreshing never extends a grant past this moment.
    absolute_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoke_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
