"""Tools that are allowed to ask for access.

Clients are registered here, in code, not by the tools themselves. There is no
dynamic client registration: an unknown tool simply cannot start a login.
Adding a tool is a reviewed code change.

All of them are *public* clients (a desktop tool cannot keep a secret), so the
protection is PKCE plus a strict redirect rule: the tool must receive the code
on a loopback address on its own machine (RFC 8252). A code can therefore never
be sent to a web address someone controls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# What a grant can do. ``knowledge:read`` searches knowledge the person can
# read; ``knowledge:write`` additionally saves notes into a space they can
# write to. A write grant does not imply read — a tool asks for exactly what
# it needs.
SCOPE_KNOWLEDGE_READ = "knowledge:read"
SCOPE_KNOWLEDGE_WRITE = "knowledge:write"
# Uploading files (PDFs, images, Office documents) is a larger power than saving
# a text note, so it is its own scope: a grant that may save notes does not
# silently gain it. It implies neither read nor write.
SCOPE_KNOWLEDGE_UPLOAD = "knowledge:upload"
SUPPORTED_SCOPES = (
    SCOPE_KNOWLEDGE_READ,
    SCOPE_KNOWLEDGE_WRITE,
    SCOPE_KNOWLEDGE_UPLOAD,
)

# Scopes granted when a request names none. Kept to read-only on purpose:
# write is a strictly larger power, so it must be asked for explicitly. This
# also keeps every pre-existing client (which either names ``knowledge:read``
# or names nothing) read-only across this change — no grant silently gains
# the ability to write.
DEFAULT_SCOPES = (SCOPE_KNOWLEDGE_READ,)


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    name: str


CLIENTS: dict[str, OAuthClient] = {
    "quberi-harness": OAuthClient(
        client_id="quberi-harness", name="DeepSeek Harness"
    ),
    # A protocol-generic client: any tool that speaks MCP (Model Context
    # Protocol), not a particular one. Codex is the first to connect, but
    # this row is not named after it, so a second MCP client does not need
    # to be squeezed into the same registration.
    "quberi-mcp": OAuthClient(client_id="quberi-mcp", name="MCP client"),
}

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# RFC 8252 §7.3 recommends an IP literal over the ``localhost`` name (a name
# can be redefined by a hosts file or a misconfigured resolver; an IP
# literal cannot). We accept it anyway because Claude Code hardcodes the
# ``localhost`` hostname for every MCP server's OAuth redirect_uri with no
# way to configure it to send an IP literal instead — a confirmed upstream
# bug/limitation (anthropics/claude-code#82096), not a client misconfiguration
# we can ask a user to fix. ``urlsplit`` lowercases ``hostname``, so this
# only ever matches the literal name, never a mixed-case bypass.
_CALLBACK_PATH = "/callback"
# Some MCP clients (observed with Codex's rmcp-client) append an opaque,
# per-server "callback ID" segment to the loopback path instead of using a
# bare ``/callback`` — this is how they disambiguate which locally-running
# OAuth flow a redirect belongs to when more than one is in flight, and it
# is independent of whether the authorization server implements RFC 9207
# issuer-bound callbacks (the id is generated client-side either way).
# Accepting this shape (rather than only the bare path) means a new MCP
# client that uses the same pattern works without a code change here; it
# is not a Codex-specific carve-out. The suffix itself stays tightly
# bounded — URL-safe characters only, short — so this still only ever
# matches "loopback callback plus an opaque id", never an arbitrary path.
_CALLBACK_ID_RE = re.compile(r"^/callback/[A-Za-z0-9_-]{1,64}$")


def get_client(client_id: str) -> OAuthClient | None:
    return CLIENTS.get(client_id)


def is_valid_redirect_uri(uri: str) -> bool:
    """Only a loopback ``/callback`` (optionally ``/callback/<opaque-id>``) is accepted.

    The port is chosen by the tool at run time (RFC 8252), so it is not
    pinned; everything else about the address must match exactly.
    """

    if not uri or len(uri) > 200:
        return False
    try:
        parts = urlsplit(uri)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "http"
        and parts.hostname in _LOOPBACK_HOSTS
        and port is not None
        and 1024 <= port <= 65535
        and (parts.path == _CALLBACK_PATH or bool(_CALLBACK_ID_RE.match(parts.path)))
        and not parts.query
        and not parts.fragment
        and not parts.username
        and not parts.password
    )


def normalise_scope(scope: str | None) -> str | None:
    """The requested scopes if all are supported, else ``None``.

    Naming no scope grants :data:`DEFAULT_SCOPES` (read-only) rather than
    everything supported: write must be requested explicitly, so widening the
    set of supported scopes never retroactively widens an existing grant.
    """

    requested = [s for s in (scope or "").split() if s]
    if not requested:
        return " ".join(DEFAULT_SCOPES)
    if any(s not in SUPPORTED_SCOPES for s in requested):
        return None
    return " ".join(sorted(set(requested)))
