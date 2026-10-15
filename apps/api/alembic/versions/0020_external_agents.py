"""external_agents — let an agent proxy to an external endpoint.

An agent is either ``kind='builtin'`` (the in-house tool loop, default)
or ``kind='external'`` (forwards the conversation to a third-party
OpenAI-compatible chat endpoint). External config (protocol / endpoint /
model / extra) lives in a JSONB blob; the API key is encrypted in its own
column, mirroring ``ai_provider_bindings.api_key_encrypted``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            server_default="builtin",
        ),
    )
    op.add_column(
        "agents",
        sa.Column(
            "external_config",
            JSONB(),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "agents",
        sa.Column("external_api_key_encrypted", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_agents_kind",
        "agents",
        "kind IN ('builtin', 'external')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_agents_kind", "agents", type_="check")
    op.drop_column("agents", "external_api_key_encrypted")
    op.drop_column("agents", "external_config")
    op.drop_column("agents", "kind")
