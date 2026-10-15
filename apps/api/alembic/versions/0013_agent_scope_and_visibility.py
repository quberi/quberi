"""Agent knowledge-scope + visibility.

Two changes:

1. ``agents`` gains three columns:
   * ``knowledge_scope_mode`` — user | intersect | fixed (default user)
   * ``scope_space_ids``      — JSONB list of space ids for intersect/fixed
   * ``visibility``           — organization | restricted (default organization)

2. ``role_bindings.resource_type`` CHECK is widened to allow ``'agent'`` so
   the existing authz machinery can carry per-agent access grants (subject
   holds ``viewer`` on ``resource_type='agent'``). No new table — we reuse
   RoleBinding wholesale.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # agents: new columns
    # ------------------------------------------------------------------
    op.add_column(
        "agents",
        sa.Column(
            "knowledge_scope_mode",
            sa.String(length=16),
            nullable=False,
            server_default="user",
        ),
    )
    op.add_column(
        "agents",
        sa.Column(
            "scope_space_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "agents",
        sa.Column(
            "visibility",
            sa.String(length=16),
            nullable=False,
            server_default="organization",
        ),
    )
    # Raw SQL for the check constraints: the metadata naming convention
    # otherwise double-expands an explicit ``ck_...`` name into
    # ``ck_agents_ck_agents_...``. Exact names keep the schema tidy and
    # match the ORM's ``__table_args__``.
    op.execute(
        "ALTER TABLE agents ADD CONSTRAINT ck_agents_knowledge_scope_mode "
        "CHECK (knowledge_scope_mode IN ('user', 'intersect', 'fixed'))"
    )
    op.execute(
        "ALTER TABLE agents ADD CONSTRAINT ck_agents_visibility "
        "CHECK (visibility IN ('organization', 'restricted'))"
    )

    # ------------------------------------------------------------------
    # role_bindings: allow resource_type='agent'
    # ------------------------------------------------------------------
    op.execute(
        "ALTER TABLE role_bindings DROP CONSTRAINT ck_role_bindings_resource_type"
    )
    op.execute(
        "ALTER TABLE role_bindings ADD CONSTRAINT ck_role_bindings_resource_type "
        "CHECK (resource_type IN ('organization', 'space', 'agent'))"
    )


def downgrade() -> None:
    # Revert the resource_type check first (will fail if any 'agent' rows
    # remain — intentional, so we don't silently orphan grants).
    op.execute(
        "ALTER TABLE role_bindings DROP CONSTRAINT ck_role_bindings_resource_type"
    )
    op.execute(
        "ALTER TABLE role_bindings ADD CONSTRAINT ck_role_bindings_resource_type "
        "CHECK (resource_type IN ('organization', 'space'))"
    )

    op.execute("ALTER TABLE agents DROP CONSTRAINT ck_agents_visibility")
    op.execute("ALTER TABLE agents DROP CONSTRAINT ck_agents_knowledge_scope_mode")
    op.drop_column("agents", "visibility")
    op.drop_column("agents", "scope_space_ids")
    op.drop_column("agents", "knowledge_scope_mode")
