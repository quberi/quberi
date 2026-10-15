"""seed permissions and system roles

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-20
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from quberi_api.domain.authz.catalog import PERMISSIONS, SYSTEM_ROLES

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Lightweight table stubs so we do not depend on the ORM models at migration
# time (the ORM will evolve; this migration must stay pinned to the schema as
# it existed at revision 0001).
# ---------------------------------------------------------------------------
permissions_t = sa.table(
    "permissions",
    sa.column("id", sa.Uuid()),
    sa.column("code", sa.String()),
    sa.column("name", sa.String()),
    sa.column("description", sa.String()),
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

    # Insert permissions and remember their generated ids so we can wire up the
    # role_permissions join in the same transaction.
    permission_id_by_code: dict[str, uuid.UUID] = {}
    for perm in PERMISSIONS:
        pid = uuid.uuid4()
        permission_id_by_code[perm.code] = pid
        bind.execute(
            permissions_t.insert().values(
                id=pid,
                code=perm.code,
                name=perm.name,
                description=perm.description,
            )
        )

    # Insert system roles (organization_id NULL => platform-wide system roles).
    role_id_by_code: dict[str, uuid.UUID] = {}
    for role in SYSTEM_ROLES:
        rid = uuid.uuid4()
        role_id_by_code[role.code] = rid
        bind.execute(
            roles_t.insert().values(
                id=rid,
                organization_id=None,
                code=role.code,
                name=role.name,
                description=role.description,
                is_system=True,
            )
        )

        for perm_code in role.permissions:
            bind.execute(
                role_permissions_t.insert().values(
                    role_id=rid,
                    permission_id=permission_id_by_code[perm_code],
                )
            )


def downgrade() -> None:
    bind = op.get_bind()

    system_role_codes = [role.code for role in SYSTEM_ROLES]
    permission_codes = [perm.code for perm in PERMISSIONS]

    # ``role_permissions`` uses ON DELETE CASCADE for both FKs, so deleting the
    # parent rows tears down the join automatically.
    bind.execute(
        roles_t.delete().where(
            sa.and_(
                roles_t.c.organization_id.is_(None),
                roles_t.c.code.in_(system_role_codes),
            )
        )
    )
    bind.execute(
        permissions_t.delete().where(permissions_t.c.code.in_(permission_codes))
    )
