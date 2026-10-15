"""Slice E · Document ingestion foundation.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-20

Adds:

* ``document_sources`` — registered connections to external doc origins.
  ``kind`` in {local, feishu, oss_aliyun, s3_aws}. ``caching_policy`` in
  {mirror, reference_only}. Credentials stored encrypted (Fernet ASCII
  ciphertext) by the service layer; ``config`` holds inspectable public
  knobs (bucket name, region, tenant id, root folder…).
* ``documents`` — the file itself. Anchored to a space (space's
  ``RoleBinding`` graph is the sole access gate). ``source_id`` is
  nullable (null == uploaded directly, no external origin), ``storage_key``
  is nullable for ``reference_only`` sources.

No data migration; both tables start empty. The ``local`` source rows are
auto-created lazily by ``services.document_sources.ensure_local_source``
on the first upload per org.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # document_sources
    # ------------------------------------------------------------------
    op.create_table(
        "document_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "credentials_encrypted", sa.String(length=4096), nullable=True
        ),
        sa.Column(
            "caching_policy",
            sa.String(length=32),
            nullable=False,
            server_default="mirror",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
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
            "kind IN ('local', 'feishu', 'oss_aliyun', 's3_aws')",
            name=op.f("ck_document_sources_kind"),
        ),
        sa.CheckConstraint(
            "caching_policy IN ('mirror', 'reference_only')",
            name=op.f("ck_document_sources_caching_policy"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'error', 'archived')",
            name=op.f("ck_document_sources_status"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_document_sources_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_document_sources_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_sources")),
        sa.UniqueConstraint(
            "organization_id",
            "kind",
            "name",
            name="uq_document_sources_organization_kind_name",
        ),
    )
    op.create_index(
        "ix_document_sources_organization_id_kind",
        "document_sources",
        ["organization_id", "kind"],
        unique=False,
    )

    # ------------------------------------------------------------------
    # documents
    # ------------------------------------------------------------------
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=400), nullable=False),
        sa.Column("mime_type", sa.String(length=200), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=True),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "external_ref",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "sync_state",
            sa.String(length=32),
            nullable=False,
            server_default="local_only",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="uploaded",
        ),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
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
        sa.CheckConstraint(
            "status IN ('uploaded', 'processing', 'failed', 'archived')",
            name=op.f("ck_documents_status"),
        ),
        sa.CheckConstraint(
            "sync_state IN ('local_only', 'synced_fresh', 'synced_stale', 'synced_broken')",
            name=op.f("ck_documents_sync_state"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_documents_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["spaces.id"],
            name=op.f("fk_documents_space_id_spaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["document_sources.id"],
            name=op.f("fk_documents_source_id_document_sources"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"],
            ["users.id"],
            name=op.f("fk_documents_uploaded_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint(
            "space_id",
            "storage_key",
            name="uq_documents_space_id_storage_key",
        ),
    )
    op.create_index(
        "ix_documents_organization_id",
        "documents",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_documents_space_id_created_at",
        "documents",
        ["space_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_documents_uploaded_by_user_id",
        "documents",
        ["uploaded_by_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_documents_source_id",
        "documents",
        ["source_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_documents_source_id", table_name="documents")
    op.drop_index("ix_documents_uploaded_by_user_id", table_name="documents")
    op.drop_index("ix_documents_space_id_created_at", table_name="documents")
    op.drop_index("ix_documents_organization_id", table_name="documents")
    op.drop_table("documents")
    op.drop_index(
        "ix_document_sources_organization_id_kind", table_name="document_sources"
    )
    op.drop_table("document_sources")
