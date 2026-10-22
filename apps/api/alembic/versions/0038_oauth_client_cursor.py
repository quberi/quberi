"""oauth_client_cursor.

Revision ID: 0038
Revises: 0037

Adds Cursor as a configured tool. Cursor connects with a client id written into the person's own
``mcp.json`` (``auth.CLIENT_ID``) and receives its code on ``http://localhost:8787/callback``, a
loopback address. Giving it its own id, instead of sharing ``quberi-mcp``, makes the person's
list of connected tools say "Cursor" rather than the generic "MCP client".

Inserted only if no tool with that id exists, so a platform admin who already added it by hand
keeps their version. Downgrade removes the row (a connection made with it stops refreshing).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLIENT_ID = "quberi-cursor"


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO oauth_clients (id, client_id, kind, name, redirect_uris, enabled, allow_mcp, note) "
            "SELECT :id, :client_id, 'registered', 'Cursor', CAST(:redirects AS jsonb), true, true, :note "
            "WHERE NOT EXISTS (SELECT 1 FROM oauth_clients WHERE client_id = :client_id)"
        ).bindparams(
            id=uuid.uuid4(),
            client_id=CLIENT_ID,
            redirects='["loopback"]',
            note="Built in. Cursor's desktop app: set auth.CLIENT_ID to this id in mcp.json; its callback is http://localhost:8787/callback.",
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM oauth_clients WHERE client_id = :client_id").bindparams(client_id=CLIENT_ID))
