"""oauth_client_config.

Revision ID: 0037
Revises: 0036

The tools allowed to ask for access used to be a list in code. They are now rows a platform
admin can edit, so a new tool (ChatGPT, the hosted Claude apps) can be allowed, or one switched
off, without a release. The rows the code used to hold are inserted here, so behaviour does not
change on upgrade, and two are added: Claude Code and ChatGPT, which identify themselves by the
URL of a Client ID Metadata Document.

``oauth_grants.client_id`` is widened because such a URL does not fit in 64 characters.

Downgrade drops the table and narrows the column back; it fails if a grant already holds a
longer client id, which is the right outcome (those grants would be cut off).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (client_id, kind, name, redirect_uris, allow_mcp, note)
_SEED = [
    ("quberi-harness", "registered", "DeepSeek Harness", ["loopback"], False, "Built in. Uses the REST API, not the MCP server."),
    (
        "quberi-mcp",
        "registered",
        "MCP client",
        ["loopback", "https://claude.ai/api/mcp/auth_callback"],
        True,
        "Built in. Any MCP tool on the person's own machine, and the hosted Claude apps' fixed callback.",
    ),
    (
        "https://claude.ai/oauth/claude-code-client-metadata",
        "metadata_url",
        "Claude Code",
        ["loopback"],
        True,
        "Claude Code identifies itself by this URL when the server advertises client ID metadata support.",
    ),
    (
        "https://chatgpt.com/oauth/{id}/client.json",
        "metadata_url",
        "ChatGPT",
        ["https://chatgpt.com/connector/oauth/{id}"],
        True,
        "ChatGPT's per-connection client. Its callback must carry the same {id}.",
    ),
]


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("redirect_uris", postgresql.JSONB(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        # Whether a token issued to this tool is accepted by the MCP server. The REST API
        # accepts every tool's token, as it always has.
        sa.Column("allow_mcp", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("client_id", name="uq_oauth_clients_client_id"),
        sa.CheckConstraint(
            "kind IN ('registered', 'metadata_url')", name=op.f("ck_oauth_clients_kind")
        ),
    )
    table = sa.table(
        "oauth_clients",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("client_id", sa.String),
        sa.column("kind", sa.String),
        sa.column("name", sa.String),
        sa.column("redirect_uris", postgresql.JSONB),
        sa.column("allow_mcp", sa.Boolean),
        sa.column("note", sa.Text),
    )
    op.bulk_insert(
        table,
        [
            {
                "id": uuid.uuid4(),
                "client_id": client_id,
                "kind": kind,
                "name": name,
                "redirect_uris": json.loads(json.dumps(redirects)),
                "allow_mcp": allow_mcp,
                "note": note,
            }
            for client_id, kind, name, redirects, allow_mcp, note in _SEED
        ],
    )
    op.alter_column(
        "oauth_grants",
        "client_id",
        existing_type=sa.String(length=64),
        type_=sa.String(length=255),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "oauth_grants",
        "client_id",
        existing_type=sa.String(length=255),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.drop_table("oauth_clients")
