"""Partial unique index on identities.primary_phone.

Makes phone number a usable account join-key: at most one identity per
phone. Partial (``WHERE primary_phone IS NOT NULL``) so the many
email-only identities with NULL phone don't collide.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX uq_identities_primary_phone "
        "ON identities (primary_phone) WHERE primary_phone IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_identities_primary_phone")
