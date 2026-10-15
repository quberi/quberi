"""guest_starter_stats - popularity counts for the guest chat's suggested questions.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-10

One row per (day, starter_key). The guest chat page offers a curated list of
suggested questions; each time a guest asks one, its row for today is
incremented. The page orders suggestions by the last 30 days of counts and
marks the most-asked ones as popular.

Only the curated key and a count are stored - no visitor-typed text, IP or
account - so nothing personal is collected.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "guest_starter_stats",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("starter_key", sa.String(length=32), nullable=False),
        sa.Column("count", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("day", "starter_key", name="pk_guest_starter_stats"),
    )


def downgrade() -> None:
    op.drop_table("guest_starter_stats")
