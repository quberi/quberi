"""Slice B · External integrations (Feishu OAuth, tokens, webhook state).

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-23

Three tables:

* ``integrations`` — one row per (org, provider) connection. Holds
  optional BYO app credentials (encrypted); when absent the platform
  default ``QUBERI_FEISHU_*`` app is used. ``status`` tracks health.

* ``integration_user_tokens`` — per-user OAuth grants. A user authorises
  Quberi to read *their* Feishu docs; we store the encrypted
  ``access_token`` + ``refresh_token`` + expiry so retrieval can act as
  that user (the crux of "permission-faithful" cross-system RAG). One
  row per (integration, user).

* ``integration_oauth_states`` — short-lived CSRF nonces minted when we
  redirect a user to Feishu's consent screen and validated on the
  callback. Rows are single-use and expire quickly.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # integrations
    # ------------------------------------------------------------------
    op.create_table(
        "integrations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
        # BYO app credentials (Fernet ciphertext). NULL → use platform
        # default app from env. Stored as JSON blob {app_id, app_secret}.
        sa.Column("app_credentials_encrypted", sa.String(length=4096), nullable=True),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
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
            "provider IN ('feishu', 'dingtalk', 'wecom')",
            name=op.f("ck_integrations_provider"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'error', 'archived')",
            name=op.f("ck_integrations_status"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_integrations_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_integrations_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integrations")),
        sa.UniqueConstraint(
            "organization_id",
            "provider",
            name="uq_integrations_organization_provider",
        ),
    )

    # ------------------------------------------------------------------
    # integration_user_tokens
    # ------------------------------------------------------------------
    op.create_table(
        "integration_user_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # Provider's identity for this user (feishu open_id / union_id).
        sa.Column("external_user_id", sa.String(length=200), nullable=True),
        sa.Column("access_token_encrypted", sa.String(length=8192), nullable=False),
        sa.Column("refresh_token_encrypted", sa.String(length=8192), nullable=True),
        sa.Column("scopes", sa.String(length=2048), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "refresh_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
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
            ["integration_id"],
            ["integrations.id"],
            name=op.f("fk_integration_user_tokens_integration_id_integrations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_integration_user_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_user_tokens")),
        sa.UniqueConstraint(
            "integration_id",
            "user_id",
            name="uq_integration_user_tokens_integration_user",
        ),
    )
    op.create_index(
        "ix_integration_user_tokens_user_id",
        "integration_user_tokens",
        ["user_id"],
        unique=False,
    )

    # ------------------------------------------------------------------
    # integration_oauth_states
    # ------------------------------------------------------------------
    op.create_table(
        "integration_oauth_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=128), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        # Where to bounce the browser after the callback completes.
        sa.Column("redirect_after", sa.String(length=1024), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_integration_oauth_states_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_integration_oauth_states_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_oauth_states")),
        sa.UniqueConstraint("state", name="uq_integration_oauth_states_state"),
    )
    op.create_index(
        "ix_integration_oauth_states_expires_at",
        "integration_oauth_states",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_integration_oauth_states_expires_at",
        table_name="integration_oauth_states",
    )
    op.drop_table("integration_oauth_states")
    op.drop_index(
        "ix_integration_user_tokens_user_id",
        table_name="integration_user_tokens",
    )
    op.drop_table("integration_user_tokens")
    op.drop_table("integrations")
