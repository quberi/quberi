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
}

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_CALLBACK_PATH = "/callback"


def get_client(client_id: str) -> OAuthClient | None:
    return CLIENTS.get(client_id)


def is_valid_redirect_uri(uri: str) -> bool:
    """Only ``http://127.0.0.1:<port>/callback`` (or ``[::1]``) is accepted.

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
        and parts.path == _CALLBACK_PATH
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
