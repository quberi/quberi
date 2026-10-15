"""Allow ``organization`` as a role-binding subject ("everyone in the org").

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-09

Enterprise-owned spaces need to be shareable with the whole organisation
without maintaining an all-staff group. A binding with
``subject_type='organization'`` and ``subject_id=<organisation id>`` grants
its role to every member of that organisation.

Only the CHECK constraint changes; no data is touched. Downgrade removes any
``organization`` bindings first, otherwise the narrower constraint could not
be re-created.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The metadata naming convention prepends ``ck_<table>_`` automatically,
    # so both drop and create receive the bare suffix (same as 0006).
    op.drop_constraint("subject_type", "role_bindings", type_="check")
    op.create_check_constraint(
        "subject_type",
        "role_bindings",
        "subject_type IN ('user', 'group', 'organization_unit', 'position', "
        "'organization', 'service_account')",
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM role_bindings WHERE subject_type = 'organization'"))
    op.drop_constraint("subject_type", "role_bindings", type_="check")
    op.create_check_constraint(
        "subject_type",
        "role_bindings",
        "subject_type IN ('user', 'group', 'organization_unit', 'position', "
        "'service_account')",
    )
