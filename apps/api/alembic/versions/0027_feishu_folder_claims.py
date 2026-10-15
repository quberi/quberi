"""feishu_folder_claims - one organisation per connected Feishu folder.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-11

All organisations share one Feishu app and a folder token is not secret, so a
second organisation that learned a token could connect the same folder. This
table records which organisation connected a folder first; later attempts from
another organisation are refused (the primary key makes that race-safe).

Existing Feishu sources are backfilled oldest-first, so if two organisations
somehow already point at one folder, the earlier one keeps the claim.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feishu_folder_claims",
        sa.Column("folder_token", sa.String(length=128), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_feishu_folder_claims_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("folder_token", name=op.f("pk_feishu_folder_claims")),
    )
    op.create_index(
        op.f("ix_feishu_folder_claims_organization_id"),
        "feishu_folder_claims",
        ["organization_id"],
    )
    # Oldest source wins when the same folder is already registered twice.
    op.execute(
        """
        INSERT INTO feishu_folder_claims (folder_token, organization_id, created_at)
        SELECT DISTINCT ON (config->>'folder_token')
               config->>'folder_token', organization_id, created_at
        FROM document_sources
        WHERE kind = 'feishu'
          AND config->>'folder_token' IS NOT NULL
          AND config->>'folder_token' <> ''
          AND length(config->>'folder_token') <= 128
        ORDER BY config->>'folder_token', created_at ASC
        ON CONFLICT (folder_token) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_feishu_folder_claims_organization_id"),
        table_name="feishu_folder_claims",
    )
    op.drop_table("feishu_folder_claims")
