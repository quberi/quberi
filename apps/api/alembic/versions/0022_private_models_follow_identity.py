"""Private AI provider bindings follow the global Identity, not a per-org User.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-03

Problem being fixed
--------------------
0021 scoped a private (BYOK) binding to ``(organization_id, user_id)`` —
i.e. to one org-scoped ``User`` row. The same human signed into a
different organisation (or their personal account) is a *different*
``User`` row pointing at the same global ``Identity``, so their private
models were invisible outside the org they were added in. That's wrong:
a private model is the person's own key, and should follow them
everywhere they go, not the organisation they happened to be in when
they added it.

Fix
---
Replace ``ai_provider_bindings.user_id`` with ``identity_id`` (FK to the
global ``identities`` table) for private bindings. A private row now has
``organization_id = NULL`` — it genuinely isn't scoped to any one org —
and ``identity_id`` set. Visibility becomes "every binding where
``identity_id`` matches the caller's identity", independent of which org
they're currently acting in. The org's own ``allow_byok`` toggle is
enforced at *read/use* time (per current org, in the service layer), not
baked into the row itself, so turning BYOK off in one org doesn't affect
the same person's visibility in another org that still allows it.

No data migration needed: as of this revision, production has zero rows
with ``user_id IS NOT NULL`` (checked directly before writing this
migration), so this is a straight column swap, not a backfill.

Three legal shapes after this migration:

  - platform default:  is_platform_default=TRUE,  organization_id IS NULL, identity_id IS NULL
  - org-shared:         is_platform_default=FALSE, organization_id IS NOT NULL, identity_id IS NULL
  - identity-private:   is_platform_default=FALSE, organization_id IS NULL, identity_id IS NOT NULL
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Defensive guard — this migration assumes no private bindings exist
    # yet (true as of writing). If that's no longer true when this runs,
    # fail loudly rather than silently losing a user's bindings.
    conn = op.get_bind()
    stray = conn.execute(
        sa.text("SELECT count(*) FROM ai_provider_bindings WHERE user_id IS NOT NULL")
    ).scalar_one()
    if stray:
        raise RuntimeError(
            f"{stray} private binding(s) already exist with user_id set — "
            "this migration only handles the empty case. Write a backfill "
            "(user_id -> identity_id via users.identity_id) before running."
        )

    op.add_column(
        "ai_provider_bindings",
        sa.Column("identity_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_ai_provider_bindings_identity_id_identities"),
        "ai_provider_bindings",
        "identities",
        ["identity_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_ai_provider_bindings_identity_id",
        "ai_provider_bindings",
        ["identity_id"],
        unique=False,
    )

    op.execute(
        "ALTER TABLE ai_provider_bindings DROP CONSTRAINT ck_ai_provider_bindings_scope"
    )
    op.execute(
        "ALTER TABLE ai_provider_bindings "
        "ADD CONSTRAINT ck_ai_provider_bindings_scope CHECK ("
        "  (is_platform_default = TRUE AND organization_id IS NULL AND identity_id IS NULL)"
        "  OR (is_platform_default = FALSE AND organization_id IS NOT NULL AND identity_id IS NULL)"
        "  OR (is_platform_default = FALSE AND organization_id IS NULL AND identity_id IS NOT NULL)"
        ")"
    )

    op.drop_index("ix_ai_provider_bindings_user_id", table_name="ai_provider_bindings")
    op.drop_constraint(
        op.f("fk_ai_provider_bindings_user_id_users"),
        "ai_provider_bindings",
        type_="foreignkey",
    )
    op.drop_column("ai_provider_bindings", "user_id")


def downgrade() -> None:
    conn = op.get_bind()
    stray = conn.execute(
        sa.text(
            "SELECT count(*) FROM ai_provider_bindings WHERE identity_id IS NOT NULL"
        )
    ).scalar_one()
    if stray:
        raise RuntimeError(
            f"{stray} identity-scoped private binding(s) exist — downgrading "
            "would silently orphan them (no org to attach user_id to). "
            "Remove or manually migrate them first."
        )

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

    op.drop_index("ix_ai_provider_bindings_identity_id", table_name="ai_provider_bindings")
    op.drop_constraint(
        op.f("fk_ai_provider_bindings_identity_id_identities"),
        "ai_provider_bindings",
        type_="foreignkey",
    )
    op.drop_column("ai_provider_bindings", "identity_id")
