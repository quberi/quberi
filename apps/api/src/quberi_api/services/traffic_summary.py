"""Summary of the reverse proxy's access log, for the platform admin's Traffic page.

The proxy writes one JSON line per request (see ``deploy/Caddyfile``). This module reads that
file, tells real people from crawlers, scanners and MCP directories, and returns counts. It
never returns a raw log line or a full address.

Read the numbers with these limits in mind:

* **The classification is a heuristic.** Kinds come from the Host, the User-Agent and the
  request pattern only. A ``browser_like`` request can still be a crawler that borrows a
  browser's User-Agent; a real person using ``curl`` is counted as a tool.
* **Engagement signals** (guest questions, sign-in attempts, OAuth requests, successful MCP
  calls) are only counted for the kinds that could be a real person or client
  (``browser_like``, ``mcp_client``, ``other``), and only for successful (2xx) POSTs, so an
  attempt that was refused is not engagement. A successful MCP call from a User-Agent that looks
  like a tool is therefore not counted.
* **Addresses.** The connecting address (``request.remote_ip``) is the CDN's when the request
  came through it. The visitor address in ``visitor_ip`` is a request header and anyone who
  reaches the server directly can forge it, so it is only used when the CDN also supplied a
  country, and every visitor says which of the two it came from. Addresses are masked.
* **User-Agents, hosts and paths are attacker-controlled text.** They are cut to a fixed length
  and returned only as JSON strings.

The functions are pure (``summarise_log`` takes the clock as an argument) so they are tested
without a running proxy. Only ``get_traffic`` touches the clock, a thread and a small cache.
"""

from __future__ import annotations

import asyncio
import gzip
import ipaddress
import json
import logging
import math
import os
import re
import stat
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

# Most decompressed log bytes read per summary. Newest data is read first; older data is dropped.
DEFAULT_BYTE_CAP = 64 * 1024 * 1024
# A line longer than this is not an access line we wrote; it is skipped, not parsed.
MAX_LINE_BYTES = 64 * 1024
CACHE_TTL_SECONDS = 30
CACHE_MAX_ENTRIES = 32
MAX_EXCLUDES = 20
MAX_EXCLUDE_LENGTH = 64
UA_MAX = 80
PATH_MAX = 120
HOST_MAX = 80
# Fields kept on a parsed entry are cut at these lengths before anything is classified.
_UA_KEEP = 512
_METHOD_MAX = 16

ADDRESS_NOTE = (
    "Addresses are masked. A visitor address taken from a request header can be forged by "
    "anyone who reaches the server directly, without going through the CDN; rows marked "
    "'remote_ip' use the connecting address."
)

# ---------------------------------------------------------------------------
# Classification rules (data, not code). All keywords are lower-case and are matched
# against the lower-cased User-Agent.
# ---------------------------------------------------------------------------

KINDS = (
    "direct_or_unknown_host",
    "search_engine",
    "mcp_directory_or_probe",
    "scanner_or_tool",
    "mcp_client",
    "browser_like",
    "other",
)
# Kinds that are never a person or a real client.
AUTOMATED_KINDS = frozenset(
    {"direct_or_unknown_host", "search_engine", "mcp_directory_or_probe", "scanner_or_tool"}
)
# Kinds whose requests can count as engagement.
HUMAN_OR_CLIENT_KINDS = frozenset({"browser_like", "mcp_client", "other"})

# Search engines and other well-known crawlers that name themselves.
SEARCH_ENGINE_WORDS = (
    "googlebot",
    "googleother",
    "google-inspectiontool",
    "adsbot-google",
    "bingbot",
    "bingpreview",
    "msnbot",
    "baiduspider",
    "yandexbot",
    "duckduckbot",
    "applebot",
    "sogou",
    "petalbot",
    "bytespider",
    "ahrefsbot",
    "semrushbot",
    "mj12bot",
    "dotbot",
    "seznambot",
    "exabot",
    "amazonbot",
    "gptbot",
    "oai-searchbot",
    "claudebot",
    "ccbot",
    "perplexitybot",
    "facebookexternalhit",
    "twitterbot",
    "linkedinbot",
    "slackbot",
)
# Directories, health checks and trust/ranking services that probe public MCP endpoints.
PROBE_WORDS = (
    "probe",
    "collector",
    "mcpbeat",
    "liveness",
    "trustbot",
    "trust bot",
    "rank",
    "verify",
    "similar",
    "healthcheck",
    "health check",
    "health-check",
    "uptime",
    "monitor",
    "pingdom",
    "tacuri",
    "golemreach",
    "protogrid",
    "rokmcp",
)
# Words any self-declared bot uses. On the MCP host they mean a directory crawler; anywhere they
# stop a Mozilla/5.0 User-Agent from being called browser-like.
GENERIC_BOT_WORDS = ("bot", "crawler", "spider", "scraper")
# Command-line tools, HTTP libraries and security scanners.
SCANNER_WORDS = (
    "curl",
    "wget",
    "python",
    "go-http",
    "libredtail",
    "java/",
    "okhttp",
    "scrapy",
    "security research",
    "nmap",
    "zgrab",
    "masscan",
    "nikto",
    "nuclei",
    "sqlmap",
    "gobuster",
    "libwww",
    "aiohttp",
)
# Tools people connect to an MCP endpoint with.
MCP_CLIENT_WORDS = ("codex", "claude", "cursor", "vscode", "mcp-client", "windsurf")
# User-Agents that are a client only when they are exactly this (a bare Node.js MCP client).
MCP_CLIENT_EXACT = ("node",)

# Requests (POST, as written in the access log) that show a person or client really used the
# product. Matched on the path with the query string already removed by the proxy.
GUEST_ASK_PATHS = frozenset({"/api/v1/guest/ask/stream"})
SIGN_IN_PATHS = frozenset(
    {
        "/api/v1/auth/request-code",
        "/api/v1/auth/verify",
        "/api/v1/auth/phone/request-code",
        "/api/v1/auth/phone/verify",
    }
)
OAUTH_PATHS = frozenset({"/api/v1/oauth/authorize", "/api/v1/oauth/token"})

SIGNAL_NAMES = ("guest_questions", "sign_in_requests", "oauth_requests", "mcp_successful_posts")

UNKNOWN_HOST_LABEL = "(unknown host)"
OTHER_HOST_LABEL = "(other host)"

_ROLLED_TS = r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.\d{3}"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogEntry:
    ts: float
    host: str
    method: str
    path: str
    status: int
    user_agent: str
    country: str
    remote_ip: str
    visitor_ip: str


def _text(value: Any, limit: int) -> str:
    """A log field as a short printable string; anything that is not text becomes ''."""
    if not isinstance(value, str):
        return ""
    value = value[:limit]
    return value if value.isprintable() else "".join(c if c.isprintable() else " " for c in value)


def parse_line(raw: str | bytes) -> Optional[LogEntry]:
    """One access-log line as an entry, or ``None`` for anything else. Never raises."""
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("msg") != "handled request":
            return None
        logger_name = data.get("logger")
        if logger_name is not None and not (
            isinstance(logger_name, str) and logger_name.startswith("http.log.access")
        ):
            return None
        ts = data.get("ts")
        status = data.get("status")
        request = data.get("request")
        if isinstance(ts, bool) or not isinstance(ts, int | float) or not math.isfinite(ts):
            return None
        if isinstance(status, bool) or not isinstance(status, int):
            return None
        if not isinstance(request, dict):
            return None
        host, method, uri = request.get("host"), request.get("method"), request.get("uri")
        if not (isinstance(host, str) and isinstance(method, str) and isinstance(uri, str)):
            return None
        path = re.split(r"[?#]", uri, maxsplit=1)[0]
        return LogEntry(
            ts=float(ts),
            host=_text(host, 255),
            method=_text(method, _METHOD_MAX).upper(),
            path=_text(path, PATH_MAX),
            status=status,
            user_agent=_text(data.get("user_agent"), _UA_KEEP),
            country=_text(data.get("country"), 8),
            remote_ip=_text(request.get("remote_ip"), 64),
            visitor_ip=_text(data.get("visitor_ip"), 64),
        )
    except Exception:  # noqa: BLE001 - a bad line (even a RecursionError) must never stop a summary
        return None


# ---------------------------------------------------------------------------
# Hosts, addresses
# ---------------------------------------------------------------------------


def normalise_host(host: str) -> str:
    """Lower-case host without port or trailing dot (``[v6]:port`` and ``v4:port`` handled)."""
    h = host.strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end != -1 else h.strip("[]")
    if h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h.rstrip(".")


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(normalise_host(host))
    except ValueError:
        return False
    return True


def site_role(host: str) -> str:
    """``mcp``, ``api`` or ``web`` from the first DNS label (``mcp.example.test`` -> ``mcp``)."""
    first = normalise_host(host).split(".", 1)[0]
    return first if first in ("mcp", "api") else "web"


def _normalise_ip(value: str) -> Optional[str]:
    """Canonical text of an address (IPv4-mapped IPv6 becomes IPv4), or ``None``."""
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def resolve_visitor(entry: LogEntry) -> tuple[str, str]:
    """``(address, source)`` of the visitor; address is ``''`` when none can be read.

    The ``visitor_ip`` header is used only when the CDN also reported a country (it always sets
    both; a request that reached the server directly normally has neither). Otherwise the
    connecting address is used. Nothing here knows the CDN's address ranges, so it cannot go stale.
    """
    header = _normalise_ip(entry.visitor_ip)
    if header is not None and entry.country not in ("", "-"):
        return header, "header"
    return _normalise_ip(entry.remote_ip) or "", "remote_ip"


def mask_address(address: str) -> str:
    """IPv4 keeps three octets (``203.0.113.x``); IPv6 keeps three groups (``2001:db8:1:…``)."""
    normal = _normalise_ip(address) if address else None
    if normal is None:
        return "unknown"
    ip = ipaddress.ip_address(normal)
    if isinstance(ip, ipaddress.IPv4Address):
        return ".".join(normal.split(".")[:3] + ["x"])
    groups = ip.exploded.split(":")[:3]
    return ":".join(f"{int(g, 16):x}" for g in groups) + ":…"


def parse_excludes(raw: str) -> frozenset[str]:
    """Addresses to leave out, from a comma list. Invalid entries are ignored; at most 20 count."""
    found: set[str] = set()
    entries = [p.strip() for p in raw.split(",") if p.strip()][:MAX_EXCLUDES]
    for item in entries:
        if len(item) > MAX_EXCLUDE_LENGTH:
            continue
        normal = _normalise_ip(item)
        if normal is not None:
            found.add(normal)
    return frozenset(found)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(w in text for w in words)


def classify(entry: LogEntry, site_hosts: frozenset[str]) -> str:
    """Which kind of visitor sent this request. First matching rule wins, in ``KINDS`` order.

    A heuristic: see the module docstring.
    """
    host = normalise_host(entry.host)
    if not host or is_ip_literal(host) or (site_hosts and host not in site_hosts):
        return "direct_or_unknown_host"
    ua = entry.user_agent.strip().lower()
    on_mcp_host = site_role(host) == "mcp"
    if _has_any(ua, SEARCH_ENGINE_WORDS):
        return "search_engine"
    if _has_any(ua, PROBE_WORDS) or (on_mcp_host and _has_any(ua, GENERIC_BOT_WORDS)):
        return "mcp_directory_or_probe"
    if not ua or _has_any(ua, SCANNER_WORDS):
        return "scanner_or_tool"
    if on_mcp_host and (_has_any(ua, MCP_CLIENT_WORDS) or ua in MCP_CLIENT_EXACT):
        return "mcp_client"
    if ua.startswith("mozilla/5.0") and not _has_any(ua, GENERIC_BOT_WORDS):
        return "browser_like"
    return "other"


def signal_for(entry: LogEntry) -> Optional[str]:
    """The engagement signal this request is, if any: a successful POST to a known endpoint."""
    if entry.method != "POST" or not 200 <= entry.status < 300:
        return None
    if site_role(entry.host) == "mcp":
        return "mcp_successful_posts"
    path = entry.path.rstrip("/") or "/"
    if path in GUEST_ASK_PATHS:
        return "guest_questions"
    if path in SIGN_IN_PATHS:
        return "sign_in_requests"
    if path in OAUTH_PATHS:
        return "oauth_requests"
    return None


# ---------------------------------------------------------------------------
# Finding and reading files
# ---------------------------------------------------------------------------


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def list_log_files(path: Path) -> list[Path]:
    """The configured log first, then its rolled siblings, newest first.

    Rolled files are named ``<stem>-<UTC timestamp><suffix>`` and are gzip-compressed by default
    (``.gz`` added), so name order is time order. When a file exists both plain and compressed
    (the proxy is compressing it) the plain one is used. Symlinks and other names are ignored.
    """
    pattern = re.compile(
        rf"^{re.escape(path.stem)}-{_ROLLED_TS}{re.escape(path.suffix)}(\.gz)?$"
    )
    rolled: dict[str, Path] = {}
    try:
        candidates = list(path.parent.iterdir())
    except OSError:
        return [path]
    for item in candidates:
        if not pattern.match(item.name) or not _is_regular_file(item):
            continue
        key = item.name[:-3] if item.name.endswith(".gz") else item.name
        if key in rolled and not rolled[key].name.endswith(".gz"):
            continue
        rolled[key] = item
    return [path] + [rolled[k] for k in sorted(rolled, reverse=True)]


class _Budget:
    """Bytes still allowed to be read; ``cut`` records that data was left unread."""

    def __init__(self, remaining: int) -> None:
        self.remaining = remaining
        self.cut = False


def _iter_lines(handle: IO[bytes], budget: _Budget) -> Iterator[bytes]:
    """Lines of an open file, spending the budget. Overlong lines are skipped, not returned."""
    while True:
        line = handle.readline(MAX_LINE_BYTES + 1)
        if not line:
            return
        if budget.remaining <= 0:
            budget.cut = True
            return
        budget.remaining -= len(line)
        if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
            while True:  # drain the rest of the overlong line
                chunk = handle.readline(MAX_LINE_BYTES + 1)
                budget.remaining -= len(chunk)
                if not chunk or chunk.endswith(b"\n"):
                    break
            continue
        yield line


def _open_log(path: Path) -> IO[bytes]:
    return gzip.open(path, "rb") if path.name.endswith(".gz") else open(path, "rb")  # noqa: SIM115


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


class _Visitor:
    __slots__ = ("count", "kinds", "country", "user_agent", "from_header")

    def __init__(self, country: str, user_agent: str) -> None:
        self.count = 0
        self.kinds: Counter[str] = Counter()
        self.country = country
        self.user_agent = user_agent
        self.from_header = False


class _Summary:
    """Running counts for one window. Everything kept is bounded by the bytes read."""

    def __init__(
        self,
        *,
        from_ts: float,
        now: float,
        exclude_ips: frozenset[str],
        site_hosts: frozenset[str],
    ) -> None:
        self.from_ts = from_ts
        self.site_hosts = site_hosts
        self.exclude_ips = exclude_ips
        self.first_hour = int(from_ts) // 3600 * 3600
        last_hour = int(now) // 3600 * 3600
        n_hours = (last_hour - self.first_hour) // 3600 + 1
        self.hour_requests = [0] * n_hours
        self.hour_automated = [0] * n_hours
        self.requests = 0
        self.automated = 0
        self.by_kind: Counter[str] = Counter()
        self.by_site: Counter[str] = Counter()
        self.by_status: Counter[str] = Counter()
        self.by_country: Counter[str] = Counter()
        self.mcp_agents: Counter[tuple[str, str]] = Counter()
        self.mcp_paths: Counter[tuple[str, str, int]] = Counter()
        self.web_pages: dict[str, list[int]] = {}
        self.signals: Counter[str] = Counter()
        self.visitors: dict[str, _Visitor] = {}

    def add(self, entry: LogEntry) -> None:
        address, source = resolve_visitor(entry)
        if self.exclude_ips and (
            address in self.exclude_ips or _normalise_ip(entry.visitor_ip) in self.exclude_ips
        ):
            return
        kind = classify(entry, self.site_hosts)
        host = normalise_host(entry.host)
        role = site_role(host)
        automated = kind in AUTOMATED_KINDS
        ua = _clip(entry.user_agent, UA_MAX)

        self.requests += 1
        self.by_kind[kind] += 1
        slot = (int(entry.ts) // 3600 * 3600 - self.first_hour) // 3600
        self.hour_requests[slot] += 1
        if automated:
            self.automated += 1
            self.hour_automated[slot] += 1

        # A raw Host on an unknown-host request may be the server's own address: never shown.
        site = UNKNOWN_HOST_LABEL if kind == "direct_or_unknown_host" else _clip(host, HOST_MAX)
        self.by_site[site] += 1
        klass = entry.status // 100
        self.by_status[f"{klass}xx" if 2 <= klass <= 5 else "other"] += 1
        country = entry.country.strip().upper()
        country = country if len(country) == 2 and country.isalpha() else "unknown"
        self.by_country[country] += 1

        if kind != "direct_or_unknown_host":
            if role == "mcp":
                self.mcp_agents[(ua, kind)] += 1
                self.mcp_paths[(entry.method, entry.path, entry.status)] += 1
            elif role == "web" and kind in ("browser_like", "search_engine"):
                counts = self.web_pages.setdefault(entry.path, [0, 0])
                counts[0 if kind == "browser_like" else 1] += 1

        signal = signal_for(entry)
        if signal is not None and kind in HUMAN_OR_CLIENT_KINDS:
            self.signals[signal] += 1

        visitor = self.visitors.get(address)
        if visitor is None:
            visitor = self.visitors[address] = _Visitor(country, ua)
        visitor.count += 1
        visitor.kinds[kind] += 1
        visitor.from_header = visitor.from_header or source == "header"

    def result(self, *, now: float, files_read: int, truncated: bool) -> dict[str, Any]:
        sites = self.by_site.most_common()
        by_site = [{"host": h, "requests": n} for h, n in sites[:20]]
        rest = sum(n for _, n in sites[20:])
        if rest:
            by_site.append({"host": OTHER_HOST_LABEL, "requests": rest})
        top_visitors = sorted(self.visitors.items(), key=lambda kv: kv[1].count, reverse=True)[:10]
        pages = sorted(self.web_pages.items(), key=lambda kv: sum(kv[1]), reverse=True)[:15]
        hourly = [
            {
                "hour": _iso(self.first_hour + i * 3600),
                "requests": self.hour_requests[i],
                "automated_requests": self.hour_automated[i],
            }
            for i in range(len(self.hour_requests))
        ]
        return {
            "enabled": True,
            "reason": None,
            "window": {"from": _iso(self.from_ts), "to": _iso(now)},
            "files_read": files_read,
            "truncated_by_cap": truncated,
            "address_note": ADDRESS_NOTE,
            "totals": {
                "requests": self.requests,
                "distinct_visitors": len(self.visitors),
                "automated_requests": self.automated,
                "automated_share": (
                    round(self.automated / self.requests, 3) if self.requests else 0.0
                ),
            },
            "by_site": by_site,
            "by_kind": [{"kind": k, "requests": self.by_kind.get(k, 0)} for k in KINDS],
            "by_status_class": [
                {"status_class": c, "requests": self.by_status.get(c, 0)}
                for c in ("2xx", "3xx", "4xx", "5xx", "other")
            ],
            "by_country": [
                {"country": c, "requests": n} for c, n in self.by_country.most_common(10)
            ],
            "mcp": {
                "top_user_agents": [
                    {"user_agent": ua, "kind": kind, "requests": n}
                    for (ua, kind), n in self.mcp_agents.most_common(10)
                ],
                "top_paths": [
                    {"method": m, "path": p, "status": s, "requests": n}
                    for (m, p, s), n in self.mcp_paths.most_common(10)
                ],
            },
            "web_pages": [
                {"path": p, "browser_like": c[0], "search_engine": c[1], "total": c[0] + c[1]}
                for p, c in pages
            ],
            "signals": {name: self.signals.get(name, 0) for name in SIGNAL_NAMES},
            "top_visitors": [
                {
                    "address": mask_address(addr),
                    "address_source": "header" if v.from_header else "remote_ip",
                    "kind": v.kinds.most_common(1)[0][0],
                    "country": v.country,
                    "user_agent": v.user_agent,
                    "requests": v.count,
                }
                for addr, v in top_visitors
            ],
            "hourly": hourly,
        }


def _disabled(reason: str, *, hours: int, now: float) -> dict[str, Any]:
    """The same shape as an enabled summary, with nothing in it."""
    empty = _Summary(
        from_ts=now - hours * 3600, now=now, exclude_ips=frozenset(), site_hosts=frozenset()
    )
    out = empty.result(now=now, files_read=0, truncated=False)
    out.update(enabled=False, reason=reason, hourly=[])
    return out


def summarise_log(
    path: str,
    *,
    hours: int,
    exclude_ips: frozenset[str],
    site_hosts: frozenset[str],
    now: float,
    byte_cap: int = DEFAULT_BYTE_CAP,
) -> dict[str, Any]:
    """Counts for the last ``hours`` hours up to ``now``, from ``path`` and its rolled files.

    Synchronous and blocking: call it from a thread. Files are read newest first and the reading
    stops when a file's newest entry is older than the window, or after ``byte_cap`` bytes (the
    newest bytes are kept and ``truncated_by_cap`` is set).
    """
    if not path.strip():
        return _disabled("not_configured", hours=hours, now=now)
    main = Path(path)
    try:
        if not stat.S_ISREG(os.stat(main).st_mode):
            return _disabled("file_missing", hours=hours, now=now)
        main_handle = _open_log(main)
    except (FileNotFoundError, NotADirectoryError):
        return _disabled("file_missing", hours=hours, now=now)
    except OSError:
        return _disabled("unreadable", hours=hours, now=now)

    from_ts = now - hours * 3600
    summary = _Summary(from_ts=from_ts, now=now, exclude_ips=exclude_ips, site_hosts=site_hosts)
    budget = _Budget(byte_cap)
    files_read = 0
    oldest_seen: Optional[float] = None
    try:
        for index, file_path in enumerate(list_log_files(main)):
            if budget.remaining <= 0:
                budget.cut = True
                break
            try:
                handle = main_handle if index == 0 else _open_log(file_path)
            except OSError:
                logger.debug("traffic: skipping unreadable rolled log %s", file_path.name)
                continue
            newest_in_file: Optional[float] = None
            try:
                with handle:
                    if not file_path.name.endswith(".gz"):
                        size = os.fstat(handle.fileno()).st_size
                        if size > budget.remaining:
                            # Keep the newest bytes: start inside the file, drop the partial line.
                            handle.seek(size - budget.remaining)
                            handle.readline(MAX_LINE_BYTES + 1)
                            budget.cut = True
                    for raw in _iter_lines(handle, budget):
                        entry = parse_line(raw)
                        if entry is None:
                            continue
                        newest_in_file = max(entry.ts, newest_in_file or entry.ts)
                        oldest_seen = min(entry.ts, oldest_seen or entry.ts)
                        if from_ts <= entry.ts <= now:
                            summary.add(entry)
            except (OSError, EOFError):
                # A damaged or vanished rolled file must not fail the page (the main one is
                # reported as unreadable only if it cannot be opened).
                logger.debug("traffic: stopped reading %s", file_path.name)
                continue
            files_read += 1
            if newest_in_file is not None and newest_in_file < from_ts:
                break  # every older file is older still
            if budget.cut:
                break
    finally:
        main_handle.close()
    # Data was left unread, and nothing read reached back past the start of the window.
    truncated = budget.cut and (oldest_seen is None or oldest_seen >= from_ts)
    return summary.result(now=now, files_read=files_read, truncated=truncated)


# ---------------------------------------------------------------------------
# Cached, off-the-event-loop entry point
# ---------------------------------------------------------------------------

_monotonic = time.monotonic
_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}


def cache_clear() -> None:
    _cache.clear()


async def get_traffic(settings: Any, *, hours: int, exclude_ips_raw: str) -> dict[str, Any]:
    """The summary for the configured log, reused for 30 s per (window, excludes)."""
    excludes = parse_excludes(exclude_ips_raw)
    hosts = frozenset(settings.traffic_hosts)
    path = settings.access_log_path
    key = (path, tuple(sorted(hosts)), hours, tuple(sorted(excludes)))
    now_mono = _monotonic()
    hit = _cache.get(key)
    if hit is not None and now_mono - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    result = await asyncio.to_thread(
        summarise_log,
        path,
        hours=hours,
        exclude_ips=excludes,
        site_hosts=hosts,
        now=time.time(),
    )
    _cache.pop(key, None)
    while len(_cache) >= CACHE_MAX_ENTRIES:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (now_mono, result)
    return result
