"""Multi-provider email · name/priority/enabled on platform_email_settings.

Turns the email-settings singleton into a set of SMTP providers so the
sender can fail over between them (e.g. a domestic 163 primary + an
overseas Gmail fallback). Existing rows get name='default', priority=0,
enabled=true.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "platform_email_settings",
        sa.Column(
            "name", sa.String(length=64), nullable=False, server_default="default"
        ),
    )
    op.add_column(
        "platform_email_settings",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "platform_email_settings",
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_email_settings", "enabled")
    op.drop_column("platform_email_settings", "priority")
    op.drop_column("platform_email_settings", "name")
