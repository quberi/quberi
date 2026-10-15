"""Slice G · Document indexing metadata.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-21

Groundwork for the coming RAG slice: each document row now carries the
state of its indexing pipeline so the UI can surface it before real
retrieval lands. The columns default to "not_indexed" everywhere — the
RAG worker will flip them as it processes new documents.

Columns added to ``documents``:

* ``indexing_state`` — one of ``not_indexed`` / ``indexing`` / ``indexed`` / ``failed``.
* ``chunk_count`` — number of embedded chunks (0 when not indexed).
* ``last_indexed_at`` — timestamp of the most recent successful index build.
* ``indexing_error`` — nullable short message when ``indexing_state == 'failed'``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "indexing_state",
            sa.String(length=32),
            nullable=False,
            server_default="not_indexed",
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "chunk_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "last_indexed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "indexing_error",
            sa.String(length=1024),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        op.f("ck_documents_indexing_state"),
        "documents",
        "indexing_state IN ('not_indexed', 'indexing', 'indexed', 'failed')",
    )
    # Common query: "which documents in this space are still un-indexed?"
    op.create_index(
        "ix_documents_space_id_indexing_state",
        "documents",
        ["space_id", "indexing_state"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_documents_space_id_indexing_state", table_name="documents"
    )
    op.drop_constraint(
        op.f("ck_documents_indexing_state"), "documents", type_="check"
    )
    op.drop_column("documents", "indexing_error")
    op.drop_column("documents", "last_indexed_at")
    op.drop_column("documents", "chunk_count")
    op.drop_column("documents", "indexing_state")
