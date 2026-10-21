"""oauth_grant_caller_label.

Revision ID: 0036
Revises: 0035

``oauth_grants.caller_label`` records which program is calling a connection, as that program
introduced itself. A person with several connections of one tool (three MCP clients, say) sees
only the registered tool name in their list of connected tools; this is what tells them apart.

Nullable, nothing backfilled: a connection fills it in the next time it connects. Older code
ignores the column. Downgrade drops it.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("oauth_grants", sa.Column("caller_label", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("oauth_grants", "caller_label")
