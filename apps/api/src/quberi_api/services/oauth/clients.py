"""Tools that are allowed to ask for access.

Which tools may ask, and where each may be sent its authorisation code, is configuration a
platform admin edits (table ``oauth_clients``), not code. There is no dynamic client
registration and nothing is fetched from a tool's URL: an unknown tool simply cannot start a
login, and what we trust about a known one is exactly what the admin wrote down.

All of them are *public* clients (a desktop tool cannot keep a secret), so the protection is
PKCE plus a strict redirect rule per tool. A rule is either ``loopback`` (the tool receives the
code on its own machine, RFC 8252, so a code can never be sent to a web address someone
controls) or an exact ``https://`` address. An https address is a different trust decision from
loopback, since it hands the code to someone's server: the hosted Claude apps' fixed callback
(Anthropic's own infrastructure) and ChatGPT's per-connection callback (OpenAI's) are the
intended cases.

A tool that identifies itself by the URL of a Client ID Metadata Document is matched on that
URL; a URL containing ``{id}`` matches any value of one short opaque segment there, and an
https redirect rule may use the same ``{id}`` to demand the same segment in the callback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.oauth.models import OAuthClientConfig
from quberi_api.services.admin_messages import CodedValueError, ref

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
    """A tool that may ask, as resolved for one request."""

    # What the tool presented. For a URL pattern this is the concrete URL.
    client_id: str
    name: str
    redirect_rules: tuple[str, ...]
    # The value of ``{id}`` captured from the client id, when the client is a URL pattern.
    captured_id: str | None = None
    # Whether the MCP server accepts a token issued to this tool.
    allow_mcp: bool = False

    def redirect_allowed(self, uri: str) -> bool:
        return redirect_allowed(self.redirect_rules, uri, captured_id=self.captured_id)


KIND_REGISTERED = "registered"
KIND_METADATA_URL = "metadata_url"
KINDS = (KIND_REGISTERED, KIND_METADATA_URL)
LOOPBACK = "loopback"
ID_PLACEHOLDER = "{id}"
# The opaque segment ``{id}`` stands for: short, URL-safe, no slashes.
_ID_CLASS = r"[A-Za-z0-9_-]{1,64}"
MAX_CLIENT_ID_LENGTH = 255

_REGISTERED_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# RFC 8252 §7.3 recommends an IP literal over the ``localhost`` name (a name can be redefined
# by a hosts file or a misconfigured resolver; an IP literal cannot). We accept it anyway
# because Claude Code hardcodes the ``localhost`` hostname for every MCP server's OAuth
# redirect_uri with no way to configure an IP literal instead (anthropics/claude-code#82096).
# ``urlsplit`` lowercases ``hostname``, so this only ever matches the literal name.
_CALLBACK_PATH = "/callback"
# Some MCP clients (observed with Codex's rmcp-client) append an opaque, per-server "callback
# ID" segment to the loopback path. The suffix stays tightly bounded: URL-safe characters only,
# short.
_CALLBACK_ID_RE = re.compile(r"^/callback/[A-Za-z0-9_-]{1,64}$")


def _loopback_ok(uri: str) -> bool:
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


def redirect_allowed(rules: tuple[str, ...] | list[str], uri: str, *, captured_id: str | None = None) -> bool:
    """Is ``uri`` one of the places this tool may be sent its code?

    The port of a loopback address is chosen by the tool at run time (RFC 8252), so it is not
    pinned; everything else must match exactly. An https rule is compared as an exact string,
    after replacing ``{id}`` with the segment captured from the client id (a rule that needs
    ``{id}`` for a client without one never matches).
    """

    if not uri or len(uri) > 200:
        return False
    for rule in rules:
        if rule == LOOPBACK:
            if _loopback_ok(uri):
                return True
            continue
        if ID_PLACEHOLDER in rule:
            if captured_id is None:
                continue
            rule = rule.replace(ID_PLACEHOLDER, captured_id)
        if uri == rule:
            return True
    return False


# ---------------------------------------------------------------------------
# What an admin may write
# ---------------------------------------------------------------------------


class ClientConfigError(CodedValueError):
    """The configuration an admin typed is not acceptable.

    Carries a message ``code`` and ``params`` (see ``services.admin_messages``) so the admin
    console can show it in its own language; ``str(exc)`` is the English text.
    """


def _check_https_url(value: str, *, subject: dict[str, Any], allow_id: bool) -> None:
    """``subject`` is a message reference naming what is being checked ("the client id URL")."""

    if len(value) > MAX_CLIENT_ID_LENGTH:
        raise ClientConfigError("admin.oauth.url_too_long", subject=subject)
    if "*" in value or " " in value:
        raise ClientConfigError("admin.oauth.url_wildcard_space", subject=subject)
    placeholder_count = value.count(ID_PLACEHOLDER)
    if placeholder_count and not allow_id:
        raise ClientConfigError(
            "admin.oauth.url_placeholder_not_allowed", subject=subject, placeholder=ID_PLACEHOLDER
        )
    if placeholder_count > 1:
        raise ClientConfigError(
            "admin.oauth.url_placeholder_once", subject=subject, placeholder=ID_PLACEHOLDER
        )
    probe = value.replace(ID_PLACEHOLDER, "x")
    if "{" in probe or "}" in probe:
        raise ClientConfigError(
            "admin.oauth.url_braces", subject=subject, placeholder=ID_PLACEHOLDER
        )
    try:
        parts = urlsplit(probe)
        port = parts.port
    except ValueError as exc:
        raise ClientConfigError("admin.oauth.url_invalid", subject=subject) from exc
    if parts.scheme != "https" or not parts.hostname:
        raise ClientConfigError("admin.oauth.url_not_https", subject=subject)
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ClientConfigError("admin.oauth.url_extras", subject=subject)
    if port is not None:
        raise ClientConfigError("admin.oauth.url_port", subject=subject)
    host = parts.hostname
    if host.replace(".", "").isdigit() or ":" in host or "." not in host:
        raise ClientConfigError("admin.oauth.url_domain", subject=subject)
    if ID_PLACEHOLDER in value:
        path_start = value.find("/", len("https://"))
        if path_start == -1 or value.index(ID_PLACEHOLDER) < path_start:
            raise ClientConfigError(
                "admin.oauth.url_placeholder_path", subject=subject, placeholder=ID_PLACEHOLDER
            )


def validate_client_config(client_id: str, kind: str, redirect_uris: list[str]) -> tuple[str, list[str]]:
    """Return the cleaned ``(client_id, redirect_uris)`` or raise :class:`ClientConfigError`."""

    client_id = (client_id or "").strip()
    if kind not in KINDS:
        raise ClientConfigError("admin.oauth.unknown_kind")
    if kind == KIND_REGISTERED:
        if not _REGISTERED_ID_RE.match(client_id):
            raise ClientConfigError("admin.oauth.registered_id_format")
    else:
        _check_https_url(client_id, subject=ref("admin.oauth.subject.client_id_url"), allow_id=True)
    rules: list[str] = []
    for raw in redirect_uris or []:
        rule = (raw or "").strip()
        if not rule:
            continue
        if rule != LOOPBACK:
            _check_https_url(
                rule, subject=ref("admin.oauth.subject.redirect", rule=repr(rule)), allow_id=True
            )
            if ID_PLACEHOLDER in rule and ID_PLACEHOLDER not in client_id:
                raise ClientConfigError(
                    "admin.oauth.redirect_needs_id", rule=repr(rule), placeholder=ID_PLACEHOLDER
                )
        if rule not in rules:
            rules.append(rule)
    if not rules:
        raise ClientConfigError("admin.oauth.redirect_required")
    return client_id, rules


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def _pattern(client_id: str) -> re.Pattern[str]:
    return re.compile("^" + re.escape(client_id).replace(re.escape(ID_PLACEHOLDER), f"(?P<id>{_ID_CLASS})") + "$")


def match_client(rows: list[OAuthClientConfig], client_id: str) -> OAuthClient | None:
    """The enabled row ``client_id`` names, exact matches first, then ``{id}`` patterns."""

    if not client_id or len(client_id) > MAX_CLIENT_ID_LENGTH:
        return None
    for row in rows:
        if row.enabled and ID_PLACEHOLDER not in row.client_id and row.client_id == client_id:
            return OAuthClient(
                client_id, row.name, tuple(row.redirect_uris or ()), allow_mcp=row.allow_mcp
            )
    for row in rows:
        if row.enabled and ID_PLACEHOLDER in row.client_id:
            found = _pattern(row.client_id).match(client_id)
            if found:
                return OAuthClient(
                    client_id,
                    row.name,
                    tuple(row.redirect_uris or ()),
                    captured_id=found.group("id"),
                    allow_mcp=row.allow_mcp,
                )
    return None


async def list_configured(session: AsyncSession) -> list[OAuthClientConfig]:
    return list(
        (
            await session.execute(
                select(OAuthClientConfig).order_by(OAuthClientConfig.created_at, OAuthClientConfig.client_id)
            )
        )
        .scalars()
        .all()
    )


async def resolve_client(session: AsyncSession, client_id: str) -> OAuthClient | None:
    """The tool a request names, or ``None`` if it is unknown or switched off."""

    if not client_id or len(client_id) > MAX_CLIENT_ID_LENGTH:
        return None
    return match_client(await list_configured(session), client_id)


async def metadata_documents_supported(session: AsyncSession) -> bool:
    """Is any tool that names itself by metadata URL switched on?

    Advertised in the server metadata, because some clients (Claude Code) switch to their
    metadata URL as soon as the server says it understands one.
    """

    return any(r.enabled and r.kind == KIND_METADATA_URL for r in await list_configured(session))


def normalise_scope(scope: str | None) -> str | None:
    """The requested scopes if all are supported, else ``None``.

    Naming no scope grants :data:`DEFAULT_SCOPES` (read-only) rather than everything supported:
    write must be requested explicitly, so widening the set of supported scopes never
    retroactively widens an existing grant.
    """

    requested = [s for s in (scope or "").split() if s]
    if not requested:
        return " ".join(DEFAULT_SCOPES)
    if any(s not in SUPPORTED_SCOPES for s in requested):
        return None
    return " ".join(sorted(set(requested)))


# ---------------------------------------------------------------------------
# What people tried that we did not recognise
# ---------------------------------------------------------------------------
#
# A tool we have not configured fails with "unknown application". The only way for an admin to
# learn what such a tool calls itself (the hosted Claude apps' metadata URL, say) is to see the
# attempt, so the last few are kept for the platform admin console. Only attempts by a signed-in
# person are kept (the consent page), the list is capped, and it expires on its own.

import json as _json
import time as _time

REJECTED_KEY = "quberi:oauth:rejected-clients"
REJECTED_KEEP = 30
REJECTED_TTL_SECONDS = 30 * 24 * 3600


def _printable(value: str, limit: int) -> str:
    return "".join(ch for ch in value if ch.isprintable())[:limit]


async def record_rejected(redis, *, client_id: str, redirect_uri: str, reason: str) -> None:
    """Remember a refused attempt (best effort: never let this break the refusal)."""

    try:
        member = _json.dumps(
            {
                "client_id": _printable(client_id, MAX_CLIENT_ID_LENGTH),
                "redirect_uri": _printable(redirect_uri, 200),
                "reason": reason,
            },
            sort_keys=True,
        )
        await redis.zadd(REJECTED_KEY, {member: _time.time()})
        await redis.zremrangebyrank(REJECTED_KEY, 0, -(REJECTED_KEEP + 1))
        await redis.expire(REJECTED_KEY, REJECTED_TTL_SECONDS)
    except Exception:  # noqa: BLE001 - diagnostics only
        return


async def recent_rejected(redis) -> list[dict]:
    """The latest refused attempts, newest first: ``client_id``, ``redirect_uri``, ``reason``, ``at``."""

    try:
        rows = await redis.zrevrange(REJECTED_KEY, 0, REJECTED_KEEP - 1, withscores=True)
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for member, score in rows:
        try:
            item = _json.loads(member)
        except ValueError:
            continue
        item["at"] = float(score)
        out.append(item)
    return out
