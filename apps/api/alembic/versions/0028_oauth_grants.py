"""oauth_grants + organizations.allow_external_tools.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-14

Lets outside tools (first: DeepSeek Harness) read a person's knowledge through
an OAuth grant. Tokens are stored only as hashes. Each organisation decides
whether to allow this at all; the switch defaults to off, so existing
organisations are unchanged until an owner or admin turns it on.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "allow_external_tools",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_table(
        "oauth_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("client_name", sa.String(length=200), nullable=False),
        sa.Column("scope", sa.String(length=200), nullable=False),
        sa.Column("access_hash", sa.String(length=64), nullable=False),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refresh_hash", sa.String(length=64), nullable=False),
        sa.Column("previous_refresh_hash", sa.String(length=64), nullable=True),
        sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(length=64), nullable=True),
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
            name=op.f("fk_oauth_grants_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_oauth_grants_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_grants")),
        sa.UniqueConstraint("access_hash", name="uq_oauth_grants_access_hash"),
        sa.UniqueConstraint("refresh_hash", name="uq_oauth_grants_refresh_hash"),
    )
    op.create_index("ix_oauth_grants_organization_id", "oauth_grants", ["organization_id"])
    op.create_index("ix_oauth_grants_user_id", "oauth_grants", ["user_id"])
    op.create_index(
        "ix_oauth_grants_previous_refresh_hash",
        "oauth_grants",
        ["previous_refresh_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_oauth_grants_previous_refresh_hash", table_name="oauth_grants")
    op.drop_index("ix_oauth_grants_user_id", table_name="oauth_grants")
    op.drop_index("ix_oauth_grants_organization_id", table_name="oauth_grants")
    op.drop_table("oauth_grants")
    op.drop_column("organizations", "allow_external_tools")
