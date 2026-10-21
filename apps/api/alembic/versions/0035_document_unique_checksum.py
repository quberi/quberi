"""document_unique_checksum.

Revision ID: 0035
Revises: 0034

One live copy of an uploaded file per space. The upload service already looks for an
identical file (same SHA-256) before it saves a new one; this index backs that up for
two uploads that arrive at the same moment.

Which rows it covers:

* only uploaded files (``storage_key`` set): a reference-only document, such as a Feishu
  page, has no stored file, and two pages may have the same text;
* not archived, and only ``approved`` or ``pending`` ones: a rejected copy does not block
  a new upload;
* not a guest sandbox upload (``attributes.guest_owner`` set): visitors share one space.

Nothing is backfilled. If a space already holds two live copies of one file, creating the
index fails; the migration says so instead of deleting anything. (Production had none when
this was written.)

Downgrade drops the index. Nothing else changes.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "uq_documents_space_checksum_live"
_PREDICATE = (
    "storage_key IS NOT NULL AND checksum_sha256 IS NOT NULL "
    "AND status <> 'archived' AND review_state IN ('approved', 'pending') "
    "AND (attributes ->> 'guest_owner') IS NULL"
)


def upgrade() -> None:
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                f"SELECT count(*) FROM (SELECT 1 FROM documents WHERE {_PREDICATE} "
                "GROUP BY space_id, checksum_sha256 HAVING count(*) > 1) t"
            )
        )
        .scalar_one()
    )
    if duplicates:
        raise RuntimeError(
            f"{duplicates} space(s) already hold the same uploaded file more than once. "
            "Remove or reject the extra copies, then run this migration again."
        )
    op.create_index(
        _INDEX,
        "documents",
        ["space_id", "checksum_sha256"],
        unique=True,
        postgresql_where=sa.text(_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="documents")
