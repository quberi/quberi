"""model_access_policies — enterprise model access governance.

Grants a principal (user / group / organization_unit / position) the
right to use a specific (provider_binding, model_id) chat model. A model
with no rows is open to the whole org; any row makes it restricted to the
listed principals (org owners/admins bypass in the service layer).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_access_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("provider_binding_id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        sa.Column("principal_type", sa.String(length=32), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "principal_type IN ('user', 'group', 'organization_unit', 'position')",
            name="ck_model_access_policies_principal_type",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_model_access_policies_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provider_binding_id"],
            ["ai_provider_bindings.id"],
            name="fk_model_access_policies_binding_id_ai_provider_bindings",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_access_policies"),
        sa.UniqueConstraint(
            "organization_id",
            "provider_binding_id",
            "model_id",
            "principal_type",
            "principal_id",
            name="uq_model_access_policies_grant",
        ),
    )
    op.create_index(
        "ix_model_access_policies_model",
        "model_access_policies",
        ["organization_id", "provider_binding_id", "model_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_model_access_policies_model", table_name="model_access_policies"
    )
    op.drop_table("model_access_policies")
