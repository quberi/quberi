"""Slice H · AI provider bindings and per-org model selections.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-22

Two tables land here:

* ``ai_provider_bindings`` — a registered credential to some LLM / embedding
  provider. Rows can be *organisation-scoped* (an org brought its own DeepSeek
  key, its own DashScope key, its own OpenAI key…) or *platform-scoped*
  (``organization_id`` is NULL and ``is_platform_default = TRUE``). Platform
  rows are auto-provisioned at API boot from ``QUBERI_DEFAULT_*`` env vars so
  every org has a working assistant even before an admin configures anything.
* ``ai_model_selections`` — the per-org, per-purpose choice of which binding
  and model to use. Purposes are ``chat`` and ``embedding`` today; ``reranker``
  slots in later without another migration because it's just an extra value
  in the check constraint.

Why not enum types
------------------
``provider_kind`` is stored as a plain ``String(32)`` with **no** check
constraint on purpose. Adding a new provider (say, ``xai``) shouldn't require
a migration — the provider registry lives in application code and is the
authoritative catalog. The DB just persists whatever string the app writes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_provider_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        # NULL organization_id + is_platform_default=TRUE means "platform
        # binding, available as a fallback to every org that hasn't
        # configured its own".
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("provider_kind", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("base_url", sa.String(length=1024), nullable=True),
        # Fernet ciphertext; ``services.security.crypto.seal`` writes here.
        sa.Column("api_key_encrypted", sa.String(length=4096), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "is_platform_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        # Room for provider-specific config: OpenAI org id, model name
        # prefix, DashScope workspace, custom headers, …
        sa.Column(
            "extra_config",
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
            "status IN ('active', 'paused', 'error', 'archived')",
            name=op.f("ck_ai_provider_bindings_status"),
        ),
        # Platform rows must have NULL org. Org rows must have non-NULL org.
        sa.CheckConstraint(
            "(is_platform_default = TRUE AND organization_id IS NULL) OR "
            "(is_platform_default = FALSE AND organization_id IS NOT NULL)",
            name=op.f("ck_ai_provider_bindings_scope"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_ai_provider_bindings_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_ai_provider_bindings_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_provider_bindings")),
    )
    # Only one platform-default binding per ``provider_kind`` (else which one
    # wins?). Partial unique index — a plain UNIQUE would collide on
    # ``organization_id=NULL`` due to Postgres NULL semantics.
    op.create_index(
        "uq_ai_provider_bindings_platform_default_kind",
        "ai_provider_bindings",
        ["provider_kind"],
        unique=True,
        postgresql_where=sa.text("is_platform_default = TRUE"),
    )
    op.create_index(
        "ix_ai_provider_bindings_organization_id",
        "ai_provider_bindings",
        ["organization_id"],
        unique=False,
    )

    op.create_table(
        "ai_model_selections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        # ``chat`` / ``embedding`` today; ``reranker`` slots in later.
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("provider_binding_id", sa.Uuid(), nullable=False),
        # Model id is a free string tied to the provider (``deepseek-chat``,
        # ``bge-m3``, ``qwen3-embedding-0.6b``, ``text-embedding-3-small``…);
        # the app-side registry validates it before write.
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
        sa.CheckConstraint(
            "purpose IN ('chat', 'embedding', 'reranker')",
            name=op.f("ck_ai_model_selections_purpose"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_ai_model_selections_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provider_binding_id"],
            ["ai_provider_bindings.id"],
            name=op.f(
                "fk_ai_model_selections_provider_binding_id_ai_provider_bindings"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_model_selections")),
        sa.UniqueConstraint(
            "organization_id",
            "purpose",
            name="uq_ai_model_selections_organization_purpose",
        ),
    )
    op.create_index(
        "ix_ai_model_selections_provider_binding_id",
        "ai_model_selections",
        ["provider_binding_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_model_selections_provider_binding_id",
        table_name="ai_model_selections",
    )
    op.drop_table("ai_model_selections")
    op.drop_index(
        "ix_ai_provider_bindings_organization_id",
        table_name="ai_provider_bindings",
    )
    op.drop_index(
        "uq_ai_provider_bindings_platform_default_kind",
        table_name="ai_provider_bindings",
    )
    op.drop_table("ai_provider_bindings")
