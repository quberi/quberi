"""Agent ORM model.

An :class:`Agent` is a *configuration* record, not code. It names a
persona the org has set up — a display name, an icon, a system prompt,
and the subset of built-in tools it may call. The runner loads one of
these and assembles a tool registry from ``enabled_tools``; the loop
logic itself is agent-agnostic.

See ``alembic/versions/0012_agents.py`` for the table rationale. Each
org gets a seeded ``is_default`` generalist (all tools) plus a couple of
template personas on first access; users can add their own.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from quberi_api.db.base import Base
from quberi_api.db.types import TimestampMixin, UUIDPk


class Agent(TimestampMixin, Base):
    """A per-organisation agent persona."""

    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_agents_status",
        ),
        CheckConstraint(
            "knowledge_scope_mode IN ('user', 'intersect', 'fixed')",
            name="ck_agents_knowledge_scope_mode",
        ),
        CheckConstraint(
            "kind IN ('builtin', 'external')",
            name="ck_agents_kind",
        ),
        CheckConstraint(
            "visibility IN ('organization', 'restricted')",
            name="ck_agents_visibility",
        ),
        Index("ix_agents_organization_id", "organization_id"),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(
        String(1000), nullable=True
    )
    # A UI icon key (e.g. "bot", "users", "book"); resolved to a glyph on
    # the frontend. Kept as an opaque string so adding icons never needs
    # a migration.
    icon: Mapped[str] = mapped_column(String(48), nullable=False, default="bot")
    # Extra instruction appended to the base runner system prompt. May be
    # empty — the base prompt already covers tool-use mechanics.
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # List of built-in tool names this agent may call. Unknown names are
    # ignored at assembly time so removing a tool from the catalogue never
    # breaks an agent row.
    enabled_tools: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )
    # How search_knowledge resolves its space set (see AGENT_KNOWLEDGE_SCOPE_MODES).
    knowledge_scope_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user"
    )
    # Space ids used by the ``intersect`` / ``fixed`` scope modes. Ignored
    # when mode is ``user``.
    scope_space_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )
    # Who may see + run this agent (see AGENT_VISIBILITY). When
    # ``restricted``, access is granted via RoleBindings on the agent.
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="organization"
    )
    # ``builtin`` runs the in-house tool loop; ``external`` forwards the
    # conversation to a third-party OpenAI-compatible endpoint.
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="builtin"
    )
    # For ``external`` agents: {protocol, endpoint, model, extra:{...}}.
    # Non-secret config only — the API key lives encrypted below.
    external_config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    # Encrypted API key for the external endpoint (mirrors provider bindings).
    external_api_key_encrypted: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    # The org's seeded generalist. Exactly one per org; cannot be deleted
    # (so there is always a working agent to fall back to).
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active"
    )
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
