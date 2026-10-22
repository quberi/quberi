"""a new version can wait for approval.

Revision ID: 0040
Revises: 0039

In a space that approves uploads, an editor's new version of a document waits for a reviewer
like a new document does: stored, not the current file, not indexed, seen only by its uploader
and the reviewers. ``review_state`` on ``document_versions`` says which. Every version that
exists already is ``approved``. A partial unique index allows at most one waiting version per
document.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_versions",
        sa.Column("review_state", sa.String(length=16), server_default="approved", nullable=False),
    )
    op.add_column("document_versions", sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=True))
    op.add_column(
        "document_versions", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("document_versions", sa.Column("review_note", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_document_versions_review_state"),
        "document_versions",
        "review_state IN ('approved', 'pending', 'rejected')",
    )
    op.create_foreign_key(
        op.f("fk_document_versions_reviewed_by_user_id_users"),
        "document_versions",
        "users",
        ["reviewed_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "uq_document_versions_one_pending",
        "document_versions",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("review_state = 'pending'"),
    )


def downgrade() -> None:
    # A version still waiting or refused was never the document's file; it goes with the column.
    op.execute("DELETE FROM document_versions WHERE review_state <> 'approved'")
    op.drop_index("uq_document_versions_one_pending", table_name="document_versions")
    op.drop_constraint(
        op.f("fk_document_versions_reviewed_by_user_id_users"),
        "document_versions",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("ck_document_versions_review_state"), "document_versions", type_="check"
    )
    op.drop_column("document_versions", "review_note")
    op.drop_column("document_versions", "reviewed_at")
    op.drop_column("document_versions", "reviewed_by_user_id")
    op.drop_column("document_versions", "review_state")
