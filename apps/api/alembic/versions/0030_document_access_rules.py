"""document_access_rules.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-30

Document-level exceptions to space permissions. ``allow`` lets a person read one
document of a space they cannot otherwise read; ``deny`` hides one document from a
person who can read the space. Deny wins. Rules are evaluated at query time, so the
table starts empty and every existing document keeps exactly the access it has today.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_access_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("effect", sa.String(length=8), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
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
            "effect IN ('allow', 'deny')",
            name=op.f("ck_document_access_rules_effect"),
        ),
        sa.CheckConstraint(
            "subject_type IN ('user', 'group', 'organization_unit', 'position', 'organization')",
            name=op.f("ck_document_access_rules_subject_type"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_document_access_rules_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_access_rules_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_document_access_rules_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_access_rules")),
        sa.UniqueConstraint(
            "document_id",
            "effect",
            "subject_type",
            "subject_id",
            name="uq_document_access_rules_rule",
        ),
    )
    op.create_index(
        "ix_document_access_rules_document_id",
        "document_access_rules",
        ["document_id"],
    )
    op.create_index(
        "ix_document_access_rules_subject",
        "document_access_rules",
        ["organization_id", "subject_type", "subject_id", "effect"],
    )


def downgrade() -> None:
    op.drop_index("ix_document_access_rules_subject", table_name="document_access_rules")
    op.drop_index("ix_document_access_rules_document_id", table_name="document_access_rules")
    op.drop_table("document_access_rules")
