"""Agent personas · per-organisation agent configuration.

One table:

* ``agents`` — a configurable agent persona owned by an organisation.
  Holds a display ``name`` + ``icon``, an appended ``system_prompt``,
  and the ``enabled_tools`` subset (JSONB list of built-in tool names)
  the agent may call. ``is_default`` marks the seeded generalist that
  the runner falls back to and that cannot be deleted.

Rows are seeded lazily by the service on first list (generalist +
template personas), so both existing and new orgs converge to a working
set without a data migration here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column(
            "icon",
            sa.String(length=48),
            nullable=False,
            server_default="bot",
        ),
        sa.Column(
            "system_prompt",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "enabled_tools",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "is_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('active', 'archived')",
            name=op.f("ck_agents_status"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_agents_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_agents_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agents")),
    )
    op.create_index(
        "ix_agents_organization_id",
        "agents",
        ["organization_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agents_organization_id", table_name="agents")
    op.drop_table("agents")
