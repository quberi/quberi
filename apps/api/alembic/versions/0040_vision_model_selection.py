"""a vision model choice, and the switch that lets pictures be described.

Revision ID: 0040
Revises: 0039

An organisation can choose which model describes its pictures (so what a picture shows can
be searched), separately from the model that answers chat. That choice is one more
``purpose`` in ``ai_model_selections``. Describing a picture sends it to the model's
company, so a second, separate switch (``organizations.image_vision_enabled``, off by
default) must also be turned on by an owner or admin. With neither, nothing is sent.

Downgrade removes the switch and any ``vision`` choices (the previous constraint would
reject them), then restores the previous constraint.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_ai_model_selections_purpose"


def upgrade() -> None:
    op.drop_constraint(op.f(_CONSTRAINT), "ai_model_selections", type_="check")
    op.create_check_constraint(
        op.f(_CONSTRAINT),
        "ai_model_selections",
        "purpose IN ('chat', 'embedding', 'reranker', 'vision')",
    )
    op.add_column(
        "organizations",
        sa.Column(
            "image_vision_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("organizations", "image_vision_enabled")
    op.execute("DELETE FROM ai_model_selections WHERE purpose = 'vision'")
    op.drop_constraint(op.f(_CONSTRAINT), "ai_model_selections", type_="check")
    op.create_check_constraint(
        op.f(_CONSTRAINT),
        "ai_model_selections",
        "purpose IN ('chat', 'embedding', 'reranker')",
    )
