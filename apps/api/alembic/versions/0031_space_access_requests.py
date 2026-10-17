"""space_access_requests.

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-31

An administrator who cannot read an enterprise space asks its owner for access; the owner
approves (which grants the role) or rejects. One pending request per person and space.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "space_access_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("requester_user_id", sa.Uuid(), nullable=False),
        sa.Column("role_code", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled')",
            name=op.f("ck_space_access_requests_status"),
        ),
        sa.CheckConstraint(
            "role_code IN ('viewer', 'editor')", name=op.f("ck_space_access_requests_role_code")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_space_access_requests_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["spaces.id"],
            name=op.f("fk_space_access_requests_space_id_spaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requester_user_id"],
            ["users.id"],
            name=op.f("fk_space_access_requests_requester_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["users.id"],
            name=op.f("fk_space_access_requests_decided_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_space_access_requests")),
    )
    op.create_index(
        "uq_space_access_requests_pending",
        "space_access_requests",
        ["space_id", "requester_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index("ix_space_access_requests_space", "space_access_requests", ["space_id", "status"])
    op.create_index(
        "ix_space_access_requests_requester",
        "space_access_requests",
        ["requester_user_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_space_access_requests_requester", table_name="space_access_requests")
    op.drop_index("ix_space_access_requests_space", table_name="space_access_requests")
    op.drop_index("uq_space_access_requests_pending", table_name="space_access_requests")
    op.drop_table("space_access_requests")
