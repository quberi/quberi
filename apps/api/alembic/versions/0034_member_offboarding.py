"""member_offboarding.

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-09

Groundwork for deactivating a member:

* ``spaces.status`` accepts ``frozen``, the state a deactivated member's personal space is
  put in. Only the space check constraint changes; ``GENERIC_STATUS`` is shared with other
  tables and stays as it is.
* ``users.session_version`` lets every web session of one member be ended at once.
* ``revoked_sessions`` remembers sessions that were signed out before they expired.

Nothing is backfilled and the new column has a default, so code from before this revision
keeps working against the upgraded database.

Downgrade note: a frozen space is turned back into ``archived`` so the older constraint
holds. Older code does not look at user status or ``frozen``, so after a downgrade the
personal space of a deactivated member can be read by its owner again and that member's
earlier sessions work again. Use the downgrade for development and rehearsals only; a
production rollback has to handle deactivated members by hand.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The metadata naming convention prepends ``ck_<table>_`` automatically,
    # so both drop and create receive the bare suffix (same as 0025).
    op.drop_constraint("status", "spaces", type_="check")
    op.create_check_constraint(
        "status", "spaces", "status IN ('active', 'archived', 'frozen')"
    )
    op.add_column(
        "users",
        sa.Column("session_version", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_table(
        "revoked_sessions",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_revoked_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_revoked_sessions")),
    )
    op.create_index("ix_revoked_sessions_expires_at", "revoked_sessions", ["expires_at"])
    op.create_index("ix_revoked_sessions_user_id", "revoked_sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_revoked_sessions_user_id", table_name="revoked_sessions")
    op.drop_index("ix_revoked_sessions_expires_at", table_name="revoked_sessions")
    op.drop_table("revoked_sessions")
    op.drop_column("users", "session_version")
    # The narrower constraint cannot hold ``frozen`` rows; ``archived`` is the nearest
    # "not active" state.
    op.execute(sa.text("UPDATE spaces SET status = 'archived' WHERE status = 'frozen'"))
    op.drop_constraint("status", "spaces", type_="check")
    op.create_check_constraint("status", "spaces", "status IN ('active', 'archived')")
