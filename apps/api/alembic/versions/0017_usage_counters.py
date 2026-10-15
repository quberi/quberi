"""usage_counters — free-quota metering per org per month.

Tracks chat messages spent against the platform-shared model so we can
enforce a monthly free allowance for orgs without their own key. One row
per (organization_id, period_start); a new month gets a new row.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_counters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column(
            "chat_messages_used",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_usage_counters_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_usage_counters"),
        sa.UniqueConstraint(
            "organization_id",
            "period_start",
            name="uq_usage_counters_org_period",
        ),
    )
    op.create_index(
        "ix_usage_counters_organization_id",
        "usage_counters",
        ["organization_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_usage_counters_organization_id", table_name="usage_counters")
    op.drop_table("usage_counters")
