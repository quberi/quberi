"""Multi-vendor platform models + per-org platform model visibility.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-08

Problem being fixed
--------------------
Two limitations surfaced while wiring up a second platform-shared chat
provider (OpenAI, alongside the existing DeepSeek default):

1. ``ai_provider_bindings`` only allowed **one** platform-default row
   per ``provider_kind`` (a unique index on that column alone). DeepSeek
   and OpenAI are both ``provider_kind='openai_compat'`` (same wire
   protocol, different company), so a second platform-default row for
   OpenAI collided with DeepSeek's. There was also no way to register
   *two keys for the same vendor* (e.g. a primary + overflow DeepSeek
   key) with a priority order between them.

2. No per-organisation control over whether platform-shared models are
   even visible in that org's model picker — some customers want their
   members to only ever see models the org itself configured (BYOK),
   never a Quberi-provided fallback, or want to curate a subset.

Fix
---
* Add ``ai_provider_bindings.vendor`` (free string: ``deepseek``,
  ``openai``, ...) and ``ai_provider_bindings.priority`` (int, lower
  tried first). Replace the old unique
  ``(provider_kind)`` partial index (platform-default rows only) with a
  **non-unique** ``(provider_kind, vendor)`` partial index — multiple
  platform keys for the same vendor are now legal; ``priority`` orders
  them.
* Add ``organizations.platform_model_visibility`` (``all`` default /
  ``none`` / ``allowlist``) and a new
  ``organization_platform_model_allowlist`` table (``organization_id``,
  ``model_id``) consulted only when the mode is ``allowlist``.

No data migration needed: every existing platform-default row gets
``vendor=NULL`` (harmless — DeepSeek is still the only platform vendor
until an admin adds another) and ``priority=100`` (the column default);
every existing org gets ``platform_model_visibility='all'``, i.e.
today's behaviour, unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- ai_provider_bindings: vendor + priority -------------------------
    op.add_column(
        "ai_provider_bindings",
        sa.Column("vendor", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "ai_provider_bindings",
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default="100",
        ),
    )

    # Drop the old unique-per-provider_kind partial index, replace with a
    # non-unique (provider_kind, vendor) partial index. Raw SQL to match
    # the index name Alembic/SQLAlchemy actually produced in 0010.
    op.drop_index(
        "uq_ai_provider_bindings_platform_default_kind",
        table_name="ai_provider_bindings",
    )
    op.create_index(
        "ix_ai_provider_bindings_platform_default_kind_vendor",
        "ai_provider_bindings",
        ["provider_kind", "vendor"],
        unique=False,
        postgresql_where=sa.text("is_platform_default = TRUE"),
    )

    # ---- organizations: platform_model_visibility ------------------------
    op.add_column(
        "organizations",
        sa.Column(
            "platform_model_visibility",
            sa.String(length=16),
            nullable=False,
            server_default="all",
        ),
    )
    op.execute(
        "ALTER TABLE organizations "
        "ADD CONSTRAINT ck_organizations_platform_model_visibility "
        "CHECK (platform_model_visibility IN ('all', 'none', 'allowlist'))"
    )

    # ---- organization_platform_model_allowlist ---------------------------
    op.create_table(
        "organization_platform_model_allowlist",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
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
            name=op.f(
                "fk_organization_platform_model_allowlist_organization_id_organizations"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_organization_platform_model_allowlist")
        ),
        sa.UniqueConstraint(
            "organization_id",
            "model_id",
            name="uq_organization_platform_model_allowlist_org_model",
        ),
    )
    op.create_index(
        "ix_organization_platform_model_allowlist_organization_id",
        "organization_platform_model_allowlist",
        ["organization_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_organization_platform_model_allowlist_organization_id",
        table_name="organization_platform_model_allowlist",
    )
    op.drop_table("organization_platform_model_allowlist")

    op.execute(
        "ALTER TABLE organizations "
        "DROP CONSTRAINT ck_organizations_platform_model_visibility"
    )
    op.drop_column("organizations", "platform_model_visibility")

    op.drop_index(
        "ix_ai_provider_bindings_platform_default_kind_vendor",
        table_name="ai_provider_bindings",
    )
    op.create_index(
        "uq_ai_provider_bindings_platform_default_kind",
        "ai_provider_bindings",
        ["provider_kind"],
        unique=True,
        postgresql_where=sa.text("is_platform_default = TRUE"),
    )

    op.drop_column("ai_provider_bindings", "priority")
    op.drop_column("ai_provider_bindings", "vendor")
