"""conversation_pictures.

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-02

The picture a person searched with in the chat is kept (in object storage) so the thread
shows it again when they come back. This table holds what is known about each one: whose it
is, which question it belongs to, what it is and where the bytes are. Nothing changes for
existing conversations: their questions simply have no picture.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_pictures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_conversation_pictures_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_pictures_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_conversation_pictures_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_conversation_pictures_owner_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_pictures")),
    )
    op.create_index(
        "uq_conversation_pictures_message_id", "conversation_pictures", ["message_id"], unique=True
    )
    op.create_index(
        "ix_conversation_pictures_conversation_id", "conversation_pictures", ["conversation_id"]
    )
    op.create_index(
        "ix_conversation_pictures_owner_user_id", "conversation_pictures", ["owner_user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_pictures_owner_user_id", table_name="conversation_pictures")
    op.drop_index("ix_conversation_pictures_conversation_id", table_name="conversation_pictures")
    op.drop_index("uq_conversation_pictures_message_id", table_name="conversation_pictures")
    op.drop_table("conversation_pictures")
