"""seed member system role

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-20

Adds the ``member`` system role — the baseline "you belong to this org"
grant. Existing organisations that only had owner/admin/editor/viewer are
now able to invite new users as members.

Editor and viewer roles are intentionally left as-is. Going forward the
invite flow rejects them at the organisation level; they will be granted
per-space by the Space members panel in a later slice.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Local snapshot of the ``member`` role and the permissions it carries.
# Kept literal in the migration so a future rename in the catalog does not
# retroactively rewrite what this revision installed.
MEMBER_ROLE_CODE = "member"
MEMBER_ROLE_NAME = "Member"
MEMBER_ROLE_DESCRIPTION = (
    "Baseline organisation membership: read org metadata, own a personal "
    "space, and create collaborative spaces. Access to any given space is "
    "granted separately at the space level."
)
MEMBER_PERMISSIONS = ("org:read", "space:create")


permissions_t = sa.table(
    "permissions",
    sa.column("id", sa.Uuid()),
    sa.column("code", sa.String()),
)

roles_t = sa.table(
    "roles",
    sa.column("id", sa.Uuid()),
    sa.column("organization_id", sa.Uuid()),
    sa.column("code", sa.String()),
    sa.column("name", sa.String()),
    sa.column("description", sa.String()),
    sa.column("is_system", sa.Boolean()),
)

role_permissions_t = sa.table(
    "role_permissions",
    sa.column("role_id", sa.Uuid()),
    sa.column("permission_id", sa.Uuid()),
)


def upgrade() -> None:
    bind = op.get_bind()

    role_id = uuid.uuid4()
    bind.execute(
        roles_t.insert().values(
            id=role_id,
            organization_id=None,
            code=MEMBER_ROLE_CODE,
            name=MEMBER_ROLE_NAME,
            description=MEMBER_ROLE_DESCRIPTION,
            is_system=True,
        )
    )

    for perm_code in MEMBER_PERMISSIONS:
        perm_id = bind.execute(
            sa.select(permissions_t.c.id).where(permissions_t.c.code == perm_code)
        ).scalar_one()
        bind.execute(
            role_permissions_t.insert().values(
                role_id=role_id,
                permission_id=perm_id,
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    # role_permissions rows drop via ON DELETE CASCADE when the role is
    # removed, so a single DELETE is enough.
    bind.execute(
        roles_t.delete().where(
            sa.and_(
                roles_t.c.organization_id.is_(None),
                roles_t.c.code == MEMBER_ROLE_CODE,
            )
        )
    )
