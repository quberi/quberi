"""document upload approval.

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-01

A space can ask for approval of uploads. A document then waits as ``pending`` (nothing is
indexed) until a reviewer approves or rejects it. Every existing document is ``approved``,
so nothing changes until a space turns the setting on.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("review_state", sa.String(length=16), server_default="approved", nullable=False),
    )
    op.add_column("documents", sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=True))
    op.add_column("documents", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("documents", sa.Column("review_note", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_documents_review_state"),
        "documents",
        "review_state IN ('approved', 'pending', 'rejected')",
    )
    op.create_foreign_key(
        op.f("fk_documents_reviewed_by_user_id_users"),
        "documents",
        "users",
        ["reviewed_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_documents_space_id_review_state",
        "documents",
        ["space_id", "review_state"],
        postgresql_where=sa.text("review_state <> 'approved'"),
    )


def downgrade() -> None:
    op.drop_index("ix_documents_space_id_review_state", table_name="documents")
    op.drop_constraint(op.f("fk_documents_reviewed_by_user_id_users"), "documents", type_="foreignkey")
    op.drop_constraint(op.f("ck_documents_review_state"), "documents", type_="check")
    op.drop_column("documents", "review_note")
    op.drop_column("documents", "reviewed_at")
    op.drop_column("documents", "reviewed_by_user_id")
    op.drop_column("documents", "review_state")
