"""Slice F · Conversation knowledge scope.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-21

Adds:

* ``conversations.scope_mode`` (default ``'auto'``) — chooses whether RAG
  searches every space the caller can see (``auto``) or only the ones
  explicitly attached via the new link table (``custom``). Retrieval-time
  access checks always apply regardless of the mode.
* ``conversation_knowledge_sources`` — many-to-many between conversations
  and additional spaces. Only consulted when the parent conversation's
  ``scope_mode == 'custom'``. Never contains the conversation's own
  anchor space (``conversations.space_id``); that's implicit.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add ``scope_mode`` to ``conversations``.
    op.add_column(
        "conversations",
        sa.Column(
            "scope_mode",
            sa.String(length=16),
            nullable=False,
            server_default="auto",
        ),
    )
    op.create_check_constraint(
        op.f("ck_conversations_scope_mode"),
        "conversations",
        "scope_mode IN ('auto', 'custom')",
    )

    # 2. Create the link table.
    op.create_table(
        "conversation_knowledge_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("added_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f(
                "fk_conversation_knowledge_sources_conversation_id_conversations"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["spaces.id"],
            name=op.f("fk_conversation_knowledge_sources_space_id_spaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["added_by_user_id"],
            ["users.id"],
            name=op.f("fk_conversation_knowledge_sources_added_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_knowledge_sources")),
    )
    op.create_index(
        "uq_conversation_knowledge_sources_pair",
        "conversation_knowledge_sources",
        ["conversation_id", "space_id"],
        unique=True,
    )
    op.create_index(
        "ix_conversation_knowledge_sources_space_id",
        "conversation_knowledge_sources",
        ["space_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_knowledge_sources_space_id",
        table_name="conversation_knowledge_sources",
    )
    op.drop_index(
        "uq_conversation_knowledge_sources_pair",
        table_name="conversation_knowledge_sources",
    )
    op.drop_table("conversation_knowledge_sources")
    op.drop_constraint(
        op.f("ck_conversations_scope_mode"), "conversations", type_="check"
    )
    op.drop_column("conversations", "scope_mode")
