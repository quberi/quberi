"""Organization.tier — product tier (personal | team).

Adds a ``tier`` column to ``organizations``. Existing rows default to
``team`` (server_default) so every already-provisioned org stays
enterprise-shaped and backwards compatible. ``personal`` marks a
self-serve single-member org whose UI hides organisation concepts.

The CHECK constraint is added via raw SQL (not ``op.create_check_constraint``)
because the metadata naming convention double-expands the constraint name
otherwise.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "tier",
            sa.String(length=16),
            nullable=False,
            server_default="team",
        ),
    )
    op.execute(
        "ALTER TABLE organizations "
        "ADD CONSTRAINT ck_organizations_tier "
        "CHECK (tier IN ('personal', 'team'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE organizations DROP CONSTRAINT ck_organizations_tier")
    op.drop_column("organizations", "tier")
