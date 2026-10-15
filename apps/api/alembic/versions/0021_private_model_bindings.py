"""Private (per-user) AI provider bindings + org BYOK toggle.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-02

Two changes:

* ``ai_provider_bindings.user_id`` — a nullable FK to ``users``. When set,
  the binding is **private**: it still carries ``organization_id`` (so
  tenant-scoped cleanup/cascade keeps working), but it is only visible to
  and usable by that one user, never surfaced to the org's own provider
  list or to org admins. This is the "bring your own key, just for me"
  slice requested alongside the admin-console split.

  The existing scope check constraint only knew about two shapes
  (platform-default XOR org-owned). It is replaced with a three-way
  constraint:

    - platform default:  is_platform_default=TRUE,  organization_id IS NULL, user_id IS NULL
    - org-shared:         is_platform_default=FALSE, organization_id IS NOT NULL, user_id IS NULL
    - user-private:       is_platform_default=FALSE, organization_id IS NOT NULL, user_id IS NOT NULL

* ``organizations.allow_byok`` — boolean, default TRUE. Lets a team admin
  turn off private BYOK for their org (compliance / cost control). Every
  existing org defaults to allowed so this ships as a no-op for current
  tenants.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_provider_bindings",
        sa.Column("user_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_ai_provider_bindings_user_id_users"),
        "ai_provider_bindings",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_ai_provider_bindings_user_id",
        "ai_provider_bindings",
        ["user_id"],
        unique=False,
    )

    # Replace the two-way scope constraint with the three-way version.
    # Raw SQL (not ``op.drop_constraint``) — the metadata naming convention
    # double-expands an already-final constraint name otherwise (same
    # gotcha documented in migration 0016).
    op.execute(
        "ALTER TABLE ai_provider_bindings DROP CONSTRAINT ck_ai_provider_bindings_scope"
    )
    op.execute(
        "ALTER TABLE ai_provider_bindings "
        "ADD CONSTRAINT ck_ai_provider_bindings_scope CHECK ("
        "  (is_platform_default = TRUE AND organization_id IS NULL AND user_id IS NULL)"
        "  OR (is_platform_default = FALSE AND organization_id IS NOT NULL AND user_id IS NULL)"
        "  OR (is_platform_default = FALSE AND organization_id IS NOT NULL AND user_id IS NOT NULL)"
        ")"
    )

    op.add_column(
        "organizations",
        sa.Column(
            "allow_byok",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("organizations", "allow_byok")

    op.execute(
        "ALTER TABLE ai_provider_bindings DROP CONSTRAINT ck_ai_provider_bindings_scope"
    )
    op.execute(
        "ALTER TABLE ai_provider_bindings "
        "ADD CONSTRAINT ck_ai_provider_bindings_scope CHECK ("
        "  (is_platform_default = TRUE AND organization_id IS NULL)"
        "  OR (is_platform_default = FALSE AND organization_id IS NOT NULL)"
        ")"
    )

    op.drop_index("ix_ai_provider_bindings_user_id", table_name="ai_provider_bindings")
    op.drop_constraint(
        op.f("fk_ai_provider_bindings_user_id_users"),
        "ai_provider_bindings",
        type_="foreignkey",
    )
    op.drop_column("ai_provider_bindings", "user_id")
