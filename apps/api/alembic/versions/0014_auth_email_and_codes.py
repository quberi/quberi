"""Auth foundation · platform email settings + verification codes.

Two tables for real login (email verification-code flow):

* ``platform_email_settings`` — singleton SMTP config the platform uses to
  send verification codes / invites. Password is Fernet-sealed. Seeded from
  env on first boot, admin-editable after.
* ``auth_verification_codes`` — short-lived one-time codes (hashed) bound to
  an email + purpose, with expiry + attempt cap.

``identities`` already carries ``primary_phone`` + ``external_identities``
supports SSO, so no identity schema change is needed here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_email_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="465"),
        sa.Column("username", sa.String(length=320), nullable=True),
        sa.Column("password_encrypted", sa.String(length=4096), nullable=True),
        sa.Column("from_address", sa.String(length=320), nullable=False),
        sa.Column(
            "use_ssl", sa.Boolean(), nullable=False, server_default=sa.text("true")
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_email_settings")),
    )

    op.create_table(
        "auth_verification_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "purpose", sa.String(length=32), nullable=False, server_default="login"
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_verification_codes")),
    )
    op.create_index(
        "ix_auth_verification_codes_email",
        "auth_verification_codes",
        ["email"],
    )
    op.create_index(
        "ix_auth_verification_codes_expires_at",
        "auth_verification_codes",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_auth_verification_codes_expires_at",
        table_name="auth_verification_codes",
    )
    op.drop_index(
        "ix_auth_verification_codes_email",
        table_name="auth_verification_codes",
    )
    op.drop_table("auth_verification_codes")
    op.drop_table("platform_email_settings")
