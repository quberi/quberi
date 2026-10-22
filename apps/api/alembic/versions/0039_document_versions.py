"""document versions.

Revision ID: 0039
Revises: 0038

A document can hold several stored files over time. ``document_versions`` keeps each one
(never changed once written) and ``documents.current_version_id`` points at the one the
document is showing now. The file columns on ``documents`` stay and keep describing the
current version, so nothing that reads a document changes.

Every document that already has a stored file gets a version 1 made from its own columns,
so existing documents look exactly like a document that was uploaded after this change.
A reference-only document (no stored file) gets none.

Downgrade drops the column and the table. The files themselves are untouched, but the
history of replaced versions is gone from the database (their stored objects remain until
cleaned up by hand).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Version 1 for every document that already has a stored file, made from the document's own
# columns. The version id is derived from the document id so the statement needs no extension
# and gives the same answer if it is read twice.
BACKFILL_VERSION_ONE = """
INSERT INTO document_versions
    (id, document_id, version_number, filename, mime_type, size_bytes,
     storage_key, checksum_sha256, created_by_user_id, created_at)
SELECT md5(d.id::text || 'version-1')::uuid, d.id, 1, d.name, d.mime_type, d.size_bytes,
       d.storage_key, d.checksum_sha256, d.uploaded_by_user_id, d.created_at
FROM documents d
WHERE d.storage_key IS NOT NULL AND d.checksum_sha256 IS NOT NULL
"""
BACKFILL_CURRENT_VERSION = """
UPDATE documents d
SET current_version_id = v.id
FROM document_versions v
WHERE v.document_id = d.id AND v.version_number = 1
"""


def upgrade() -> None:
    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=400), nullable=False),
        sa.Column("mime_type", sa.String(length=200), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_versions_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_document_versions_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_versions")),
        sa.UniqueConstraint(
            "document_id", "version_number", name="uq_document_versions_document_number"
        ),
    )
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])
    op.add_column("documents", sa.Column("current_version_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_documents_current_version_id",
        "documents",
        "document_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(BACKFILL_VERSION_ONE)
    op.execute(BACKFILL_CURRENT_VERSION)


def downgrade() -> None:
    op.drop_constraint("fk_documents_current_version_id", "documents", type_="foreignkey")
    op.drop_column("documents", "current_version_id")
    op.drop_index("ix_document_versions_document_id", table_name="document_versions")
    op.drop_table("document_versions")
