"""Conversation domain models.

Design notes
------------
* Every conversation is anchored to exactly one ``Space``. Governance
  (visibility, trust level, retention) is inherited from the space; the
  conversation itself does not redecide those fields.
* ``organization_id`` is denormalised onto both ``Conversation`` and
  ``Message`` so tenant-scoped queries never need a join through ``Space``.
* ``content`` is JSONB. For MVP it is ``{"type": "text", "text": "..."}``;
  the shape can grow (attachments, tool calls, structured citations) without
  a schema change.
* ``author_user_id`` is a nullable FK. It is populated only when
  ``role='user'``; assistant / system / tool messages leave it NULL.
* ``last_message_at`` + ``message_count`` are denormalised counters kept in
  sync by the service layer, so the conversation list can render quickly
  without a per-row aggregate query.
* ``tokens`` / ``cost`` / ``correlation_id`` are reserved for the RAG slice;
  today they carry zeros or NULLs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from quberi_api.db.base import Base
from quberi_api.db.types import TimestampMixin, UUIDPk
from quberi_api.domain._constraints import in_values
from quberi_api.domain.enums import (
    CONVERSATION_SCOPE_MODES,
    CONVERSATION_STATUS,
    MESSAGE_ROLES,
    MESSAGE_STATUS,
)


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        in_values("status", CONVERSATION_STATUS, name="status"),
        in_values("scope_mode", CONVERSATION_SCOPE_MODES, name="scope_mode"),
        Index("ix_conversations_organization_id", "organization_id"),
        Index(
            "ix_conversations_space_id_last_message_at",
            "space_id",
            "last_message_at",
        ),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("spaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    # Knowledge scope; ``auto`` (default) means "search every space the
    # caller has viewer+ access to at query time". ``custom`` restricts
    # retrieval to spaces explicitly linked via
    # ``ConversationKnowledgeSource`` — still re-filtered against live
    # access at query time.
    scope_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="auto"
    )
    last_message_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    message_count: Mapped[int] = mapped_column(nullable=False, default=0)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class ConversationKnowledgeSource(Base):
    """A space attached to a conversation as an extra knowledge source.

    Only meaningful when ``Conversation.scope_mode == 'custom'``. In
    ``auto`` mode this table is bypassed — the RAG pipeline derives the
    scope from ``authz.list_accessible_space_ids(user)`` at query time.

    We do NOT store the conversation's *anchor* space here — that's
    ``Conversation.space_id``. This table holds only the ADDITIONAL
    spaces the user wants to broaden the search into.
    """

    __tablename__ = "conversation_knowledge_sources"
    __table_args__ = (
        Index(
            "uq_conversation_knowledge_sources_pair",
            "conversation_id",
            "space_id",
            unique=True,
        ),
        Index(
            "ix_conversation_knowledge_sources_space_id",
            "space_id",
        ),
    )

    id: Mapped[UUIDPk]
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("spaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    added_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class Message(Base):
    """Immutable message row. No ``updated_at``: rewrites happen via new rows."""

    __tablename__ = "messages"
    __table_args__ = (
        in_values("role", MESSAGE_ROLES, name="role"),
        in_values("status", MESSAGE_STATUS, name="status"),
        Index(
            "ix_messages_conversation_id_created_at",
            "conversation_id",
            "created_at",
        ),
        Index("ix_messages_organization_id", "organization_id"),
    )

    id: Mapped[UUIDPk]
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    author_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    tokens: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    cost: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    correlation_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="completed")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
