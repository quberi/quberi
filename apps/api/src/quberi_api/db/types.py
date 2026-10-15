"""Shared column types and ORM mixins."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from sqlalchemy import DateTime, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

UUIDPk = Annotated[
    uuid.UUID,
    mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4),
]
"""UUID primary key, generated client-side so we can reference it immediately."""

UUIDColumn = Annotated[uuid.UUID, mapped_column(Uuid(as_uuid=True))]
"""Non-primary UUID column (nullable/foreign key concerns handled at column site)."""


class TimestampMixin:
    """Adds standard created_at / updated_at columns backed by DB clock."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
