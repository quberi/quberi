"""model_access_policies.effect — allow/deny two-pass access governance.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-03

Problem being fixed
--------------------
``model_access_policies`` only ever expressed "allow" — a model with any
policy row restricted it to the listed principals, with no way to exclude
one specific person from an otherwise-allowed group/org-unit/position. If
someone was a member of an allowed group but should NOT get access, the
only workaround was moving them out of the group entirely — not
acceptable when group membership also drives other things (spaces, other
models' access).

Fix
---
Add ``effect`` (``allow`` | ``deny``, default ``allow``) to each policy
row. Evaluation becomes two-pass: a ``deny`` row always wins over any
``allow`` row for the same user, regardless of which principal matched.
Existing rows all default to ``allow`` — this is a pure behaviour
addition, not a change to how any existing policy currently evaluates
(every row that exists today still means "allow", exactly as before).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_access_policies",
        sa.Column(
            "effect",
            sa.String(length=8),
            nullable=False,
            server_default="allow",
        ),
    )
    # Raw SQL (not ``op.create_check_constraint``) — the metadata naming
    # convention double-expands an already-final constraint name otherwise
    # (same gotcha documented in migrations 0016/0021).
    op.execute(
        "ALTER TABLE model_access_policies "
        "ADD CONSTRAINT ck_model_access_policies_effect "
        "CHECK (effect IN ('allow', 'deny'))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE model_access_policies "
        "DROP CONSTRAINT ck_model_access_policies_effect"
    )
    op.drop_column("model_access_policies", "effect")
