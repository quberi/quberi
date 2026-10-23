"""The Traffic page's log reader: parsing, the visitor kinds, and the summary.

Log lines here are written to ``tmp_path`` in the shape the reverse proxy writes them. Every
address is from a documentation range and every host is an ``example.test`` name.
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from quberi_api.core.config import get_settings
from quberi_api.services import traffic_summary as ts

NOW = datetime(2026, 10, 23, 8, 10, 0, tzinfo=timezone.utc).timestamp()
SITE_HOSTS = frozenset(
    {"app.example.test", "api.example.test", "mcp.example.test", "example.test"}
)
CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
GOOGLEBOT_PHONE = (
    "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36 "
    "(compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
)
VISITOR = "203.0.113.9"
EDGE = "198.51.100.7"


def line(
    *,
    ts: float = NOW - 60,
    host: str = "app.example.test",
    method: str = "GET",
    uri: str = "/",
    status: int = 200,
    ua: str = CHROME,
    remote_ip: str = EDGE,
    visitor_ip: str = VISITOR,
    country: str = "US",
    msg: str = "handled request",
    drop: tuple[str, ...] = (),
) -> str:
    """One access-log line as the proxy writes it."""
    data = {
        "level": "info",
        "ts": ts,
        "logger": "http.log.access.log2",
        "msg": msg,
        "request": {
            "remote_ip": remote_ip,
            "remote_port": "9789",
            "client_ip": remote_ip,
            "proto": "HTTP/2.0",
            "method": method,
            "host": host,
            "uri": uri,
        },
        "bytes_read": 2,
        "user_id": "",
        "duration": 0.0155,
        "size": 68,
        "status": status,
        "user_agent": ua,
        "country": country,
        "visitor_ip": visitor_ip,
    }
    for key in drop:
        if key in data["request"]:
            del data["request"][key]
        else:
            data.pop(key, None)
    return json.dumps(data)


def entry(**over) -> ts.LogEntry:
    parsed = ts.parse_line(line(**over))
    assert parsed is not None
    return parsed


def write_log(path, lines) -> None:
    path.write_text("\n".join(lines) + "\n")


def write_gz(path, lines) -> None:
    with gzip.open(path, "wt") as f:
        f.write("\n".join(lines) + "\n")


def summarise(path, *, hours=24, exclude=(), hosts=SITE_HOSTS, **kw):
    return ts.summarise_log(
        str(path),
        hours=hours,
        exclude_ips=frozenset(exclude),
        site_hosts=hosts,
        now=NOW,
        **kw,
    )


@pytest.fixture(autouse=True)
def _fresh_cache():
    ts.cache_clear()
    yield
    ts.cache_clear()


# ---------------------------------------------------------------------------
# parse_line
# ---------------------------------------------------------------------------


def test_parse_a_valid_line():
    e = ts.parse_line(line(uri="/docs?token=abc#frag", method="post", status=401, ua="curl/8.7.1"))
    assert e is not None
    assert (e.host, e.method, e.path, e.status) == ("app.example.test", "POST", "/docs", 401)
    assert (e.user_agent, e.country, e.remote_ip, e.visitor_ip) == (
        "curl/8.7.1",
        "US",
        EDGE,
        VISITOR,
    )
    assert e.ts == NOW - 60


def test_parse_accepts_bytes():
    assert ts.parse_line(line().encode()) is not None


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not json",
        '{"msg": "handled request"',
        "[1, 2, 3]",
        '"a string"',
        "null",
        "[" * 200_000 + "]" * 200_000,
        b"\xff\xfe\x00",
    ],
)
def test_parse_skips_things_that_are_not_log_lines(raw):
    assert ts.parse_line(raw) is None


def test_parse_skips_other_messages_and_loggers():
    assert ts.parse_line(line(msg="shutting down")) is None
    other = json.loads(line())
    other["logger"] = "http.handlers.reverse_proxy"
    assert ts.parse_line(json.dumps(other)) is None


@pytest.mark.parametrize(
    "missing", [("request",), ("status",), ("ts",), ("host",), ("method",), ("uri",)]
)
def test_parse_skips_lines_missing_required_fields(missing):
    assert ts.parse_line(line(drop=missing)) is None


def test_parse_skips_wrong_types():
    for key, value in (("ts", "yesterday"), ("ts", True), ("status", "200"), ("status", 2.5)):
        data = json.loads(line())
        data[key] = value
        assert ts.parse_line(json.dumps(data)) is None
    data = json.loads(line())
    data["request"] = "GET /"
    assert ts.parse_line(json.dumps(data)) is None


def test_parse_defaults_optional_fields_to_empty():
    e = ts.parse_line(line(drop=("user_agent", "country", "visitor_ip", "remote_ip")))
    assert e is not None
    assert (e.user_agent, e.country, e.visitor_ip, e.remote_ip) == ("", "", "", "")


def test_parse_tolerates_a_missing_logger_name():
    assert ts.parse_line(line(drop=("logger",))) is not None


def test_parse_cuts_long_and_unprintable_text():
    e = entry(ua="x" * 5000, uri="/" + "p" * 5000)
    assert len(e.user_agent) <= 512 and len(e.path) <= ts.PATH_MAX
    assert "\x00" not in entry(ua="bad\x00agent\x1b[31m").user_agent


# ---------------------------------------------------------------------------
# hosts, visitor address, masking, excludes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("App.Example.test", "app.example.test"),
        ("app.example.test:8443", "app.example.test"),
        ("app.example.test.", "app.example.test"),
        ("192.0.2.5:443", "192.0.2.5"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("2001:db8::1", "2001:db8::1"),
        ("", ""),
    ],
)
def test_normalise_host(host, expected):
    assert ts.normalise_host(host) == expected


def test_site_role_comes_from_the_first_label():
    assert ts.site_role("mcp.example.test") == "mcp"
    assert ts.site_role("API.example.test:443") == "api"
    assert ts.site_role("app.example.test") == "web"
    assert ts.site_role("example.test") == "web"


def test_the_visitor_header_is_trusted_only_with_a_country():
    trusted = ts.resolve_visitor(entry())
    assert trusted == (VISITOR, "header")
    for country in ("", "-"):
        assert ts.resolve_visitor(entry(country=country)) == (EDGE, "remote_ip")
    assert ts.resolve_visitor(entry(visitor_ip="not an address")) == (EDGE, "remote_ip")
    assert ts.resolve_visitor(entry(country="", remote_ip="garbage")) == ("", "remote_ip")


@pytest.mark.parametrize(
    ("address", "masked"),
    [
        ("203.0.113.9", "203.0.113.x"),
        ("2001:db8:1:2::9", "2001:db8:1:…"),
        ("2001:0db8:0001:0002:0000:0000:0000:0009", "2001:db8:1:…"),
        ("::ffff:192.0.2.5", "192.0.2.x"),
        ("2001:db8::1", "2001:db8:0:…"),
        ("garbage", "unknown"),
        ("", "unknown"),
    ],
)
def test_mask_address(address, masked):
    assert ts.mask_address(address) == masked


def test_parse_excludes_normalises_and_ignores_the_invalid():
    got = ts.parse_excludes(" 203.0.113.9, 2001:DB8:0:0:0:0:0:1 ,nonsense,::ffff:192.0.2.5,,")
    assert got == {"203.0.113.9", "2001:db8::1", "192.0.2.5"}


def test_parse_excludes_keeps_at_most_twenty_entries():
    raw = ",".join(f"192.0.2.{i}" for i in range(1, 31))
    got = ts.parse_excludes(raw)
    assert len(got) == ts.MAX_EXCLUDES
    assert "192.0.2.20" in got and "192.0.2.21" not in got
    assert ts.parse_excludes("1" * 100) == frozenset()


# ---------------------------------------------------------------------------
# classify: one fixture per category
# ---------------------------------------------------------------------------

WEB = "app.example.test"
MCP = "mcp.example.test"

CASES = [
    # direct_or_unknown_host
    ("192.0.2.50", "Mozilla/5.0", "direct_or_unknown_host"),
    ("192.0.2.50:443", CHROME, "direct_or_unknown_host"),
    ("[2001:db8::1]:443", CHROME, "direct_or_unknown_host"),
    ("", CHROME, "direct_or_unknown_host"),
    ("unrelated.invalid", CHROME, "direct_or_unknown_host"),
    ("192.0.2.50", GOOGLEBOT_PHONE, "direct_or_unknown_host"),
    # search_engine
    (WEB, GOOGLEBOT_PHONE, "search_engine"),
    (WEB, "Mozilla/5.0 (compatible; bingbot/2.0; +https://example.test/bingbot)", "search_engine"),
    (MCP, "Mozilla/5.0 (compatible; Baiduspider/2.0)", "search_engine"),
    # mcp_directory_or_probe
    (MCP, "mcpbeat/0.1 (+https://example.test/bot/; liveness check)", "mcp_directory_or_probe"),
    (MCP, "rokmcp-collector/0.2 (+https://example.test/bot)", "mcp_directory_or_probe"),
    (MCP, "AgentRankProbe/0.1", "mcp_directory_or_probe"),
    (MCP, "agentprobe/0.1.0", "mcp_directory_or_probe"),
    (MCP, "io.verifymcp/probe", "mcp_directory_or_probe"),
    (MCP, "GolemreachTrustBot/0.1", "mcp_directory_or_probe"),
    (MCP, "protogrid-probe/0.1", "mcp_directory_or_probe"),
    (MCP, "agent-collector/0.1", "mcp_directory_or_probe"),
    (MCP, "Tacuri AI (example.test) 0.1.0", "mcp_directory_or_probe"),
    (MCP, "somebot/1.0", "mcp_directory_or_probe"),
    (WEB, "mcpbeat/0.1", "mcp_directory_or_probe"),
    # scanner_or_tool
    (WEB, "curl/8.7.1", "scanner_or_tool"),
    (MCP, "libredtail-http", "scanner_or_tool"),
    (WEB, "python-requests/2.32.3", "scanner_or_tool"),
    (WEB, "Go-http-client/1.1", "scanner_or_tool"),
    (WEB, "Software Security Research/1.0 (+https://example.test/research)", "scanner_or_tool"),
    (WEB, "", "scanner_or_tool"),
    (MCP, "   ", "scanner_or_tool"),
    # mcp_client
    (MCP, "codex-mcp-client/0.163.0-alpha.11", "mcp_client"),
    (MCP, "node", "mcp_client"),
    (MCP, "claude", "mcp_client"),
    (MCP, "cursor", "mcp_client"),
    (MCP, "Visual Studio Code vscode/1.99", "mcp_client"),
    # browser_like
    (WEB, CHROME, "browser_like"),
    ("api.example.test", CHROME, "browser_like"),
    ("example.test", CHROME, "browser_like"),
    # other
    (WEB, "node", "other"),
    (WEB, "claude", "other"),
    (WEB, "somebot/1.0", "other"),
    (WEB, "Mozilla/5.0 (compatible; SomeBot/1.0)", "other"),
    (WEB, "Dart/3.4 (dart:io)", "other"),
    (MCP, "Dart/3.4 (dart:io)", "other"),
]


@pytest.mark.parametrize(("host", "ua", "kind"), CASES)
def test_classify(host, ua, kind):
    assert ts.classify(entry(host=host, ua=ua), SITE_HOSTS) == kind


def test_classify_without_configured_hosts_only_flags_address_and_empty_hosts():
    assert ts.classify(entry(host="unrelated.invalid"), frozenset()) == "browser_like"
    assert ts.classify(entry(host="192.0.2.5"), frozenset()) == "direct_or_unknown_host"


def test_every_kind_has_a_case_and_a_classification_rule():
    assert {kind for _, _, kind in CASES} == set(ts.KINDS)
    assert set(ts.KINDS) > ts.AUTOMATED_KINDS


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "uri", "status", "signal"),
    [
        (WEB, "/api/v1/guest/ask/stream", 200, "guest_questions"),
        (WEB, "/api/v1/guest/ask/stream/", 200, "guest_questions"),
        (WEB, "/api/v1/guest/ask/stream", 401, None),
        (WEB, "/api/v1/guest/ask/stream", 429, None),
        (WEB, "/api/v1/auth/request-code", 200, "sign_in_requests"),
        (WEB, "/api/v1/auth/verify", 200, "sign_in_requests"),
        (WEB, "/api/v1/auth/phone/request-code", 204, "sign_in_requests"),
        (WEB, "/api/v1/auth/phone/verify", 200, "sign_in_requests"),
        (WEB, "/api/v1/auth/guest", 200, None),
        (WEB, "/api/v1/oauth/authorize", 200, "oauth_requests"),
        (WEB, "/api/v1/oauth/token", 200, "oauth_requests"),
        (WEB, "/api/v1/oauth/token", 400, None),
        (WEB, "/api/v1/oauth/consent", 200, None),
        (MCP, "/", 200, "mcp_successful_posts"),
        (MCP, "/", 202, "mcp_successful_posts"),
        (MCP, "/", 401, None),
        (WEB, "/", 200, None),
    ],
)
def test_signal_for_posts(host, uri, status, signal):
    assert ts.signal_for(entry(host=host, uri=uri, status=status, method="POST")) == signal


def test_signal_needs_a_post():
    assert ts.signal_for(entry(uri="/api/v1/guest/ask/stream", method="GET")) is None
    assert ts.signal_for(entry(host=MCP, method="GET", status=200)) is None


def test_signals_are_counted_for_people_and_clients_not_for_bots(tmp_path):
    ask = "/api/v1/guest/ask/stream"
    write_log(
        tmp_path / "access.log",
        [
            line(method="POST", uri=ask),
            line(method="POST", uri=ask, status=401),
            line(method="POST", uri=ask, ua="curl/8.7.1"),
            line(method="POST", uri=ask, ua=GOOGLEBOT_PHONE),
            line(method="POST", uri="/api/v1/auth/request-code"),
            line(method="POST", uri="/api/v1/oauth/token"),
            line(host=MCP, method="POST", ua="codex-mcp-client/0.163.0"),
            line(host=MCP, method="POST", ua="codex-mcp-client/0.163.0", status=401),
            line(host=MCP, method="POST", ua="mcpbeat/0.1"),
        ],
    )
    got = summarise(tmp_path / "access.log")["signals"]
    assert got == {
        "guest_questions": 1,
        "sign_in_requests": 1,
        "oauth_requests": 1,
        "mcp_successful_posts": 1,
    }


# ---------------------------------------------------------------------------
# summary: window, excludes, files, cap, hourly
# ---------------------------------------------------------------------------


def test_only_lines_inside_the_window_count(tmp_path):
    write_log(
        tmp_path / "access.log",
        [
            line(ts=NOW - 25 * 3600),  # before the window
            line(ts=NOW - 23 * 3600),
            line(ts=NOW - 60),
            line(ts=NOW + 600),  # after "now"
            "garbage that is not json",
            line(msg="other message"),
        ],
    )
    got = summarise(tmp_path / "access.log", hours=24)
    assert got["totals"]["requests"] == 2
    assert got["window"] == {"from": "2026-10-22T08:10:00Z", "to": "2026-10-23T08:10:00Z"}
    assert summarise(tmp_path / "access.log", hours=1)["totals"]["requests"] == 1


def test_exclude_ips_drop_requests_before_counting(tmp_path):
    write_log(
        tmp_path / "access.log",
        [
            line(visitor_ip="203.0.113.9"),
            line(visitor_ip="203.0.113.10"),
            line(visitor_ip="2001:0db8:0:0:0:0:0:1"),
            line(country="", visitor_ip="", remote_ip="192.0.2.77"),  # no header: remote_ip used
            line(visitor_ip="203.0.113.11", remote_ip="192.0.2.88"),  # edge address is not excluded
        ],
    )
    path = tmp_path / "access.log"
    assert summarise(path)["totals"]["requests"] == 5
    got = summarise(path, exclude=ts.parse_excludes("203.0.113.9,2001:db8::1,192.0.2.77,junk"))
    assert got["totals"]["requests"] == 2
    assert got["totals"]["distinct_visitors"] == 2
    assert summarise(path, exclude=ts.parse_excludes("192.0.2.88"))["totals"]["requests"] == 5


def test_rolled_files_are_read_newest_first_and_reading_stops_at_the_window(tmp_path):
    write_log(tmp_path / "access.log", [line(uri="/main")])
    write_log(tmp_path / "access-2026-10-23T06-00-00.000.log", [line(uri="/plain", ts=NOW - 7200)])
    write_gz(
        tmp_path / "access-2026-10-23T02-00-00.000.log.gz",
        [line(uri="/gz", ts=NOW - 3600 * 5), line(uri="/gz-old", ts=NOW - 3600 * 30)],
    )
    # Named oldest, but holding an in-window line (a misfiled one): read only if nothing stops first
    write_log(
        tmp_path / "access-2026-10-20T02-00-00.000.log",
        [line(uri="/ancient", ts=NOW - 3600 * 80), line(uri="/misfiled", ts=NOW - 60)],
    )
    # Every file's newest line is inside the window, so every file is read.
    got = summarise(tmp_path / "access.log", hours=24)
    pages = {p["path"] for p in got["web_pages"]}
    assert {"/main", "/plain", "/gz", "/misfiled"} == pages
    assert got["files_read"] == 4

    # An all-old file between them ends the read: the file after it is never opened.
    all_old = [line(uri="/old", ts=NOW - 3600 * 40)]
    write_log(tmp_path / "access-2026-10-22T02-00-00.000.log", all_old)
    got = summarise(tmp_path / "access.log", hours=24)
    pages = {p["path"] for p in got["web_pages"]}
    assert "/misfiled" not in pages and "/gz" in pages
    assert got["files_read"] == 4  # main, plain, gz, and the all-old one that ended the read


def test_a_plain_and_compressed_copy_of_one_file_count_once(tmp_path):
    write_log(tmp_path / "access.log", [line(uri="/main")])
    rolled = [line(uri="/rolled", ts=NOW - 7200)]
    write_log(tmp_path / "access-2026-10-23T06-00-00.000.log", rolled)
    write_gz(tmp_path / "access-2026-10-23T06-00-00.000.log.gz", rolled)
    got = summarise(tmp_path / "access.log")
    assert got["totals"]["requests"] == 2
    assert got["files_read"] == 2


def test_a_damaged_compressed_file_is_skipped(tmp_path):
    write_log(tmp_path / "access.log", [line(uri="/main")])
    (tmp_path / "access-2026-10-23T06-00-00.000.log.gz").write_bytes(b"this is not gzip data")
    write_gz(tmp_path / "access-2026-10-23T04-00-00.000.log.gz", [line(uri="/ok", ts=NOW - 7200)])
    got = summarise(tmp_path / "access.log")
    assert got["enabled"] is True
    assert got["totals"]["requests"] == 2
    assert got["files_read"] == 2


def test_unrelated_names_and_symlinks_are_ignored(tmp_path):
    write_log(tmp_path / "access.log", [line(uri="/main")])
    write_log(tmp_path / "access-notes.txt", [line(uri="/notes")])
    write_log(tmp_path / "access.log.1", [line(uri="/numbered")])
    write_log(tmp_path / "other-2026-10-23T06-00-00.000.log", [line(uri="/other")])
    target = tmp_path / "elsewhere.txt"
    write_log(target, [line(uri="/linked")])
    os.symlink(target, tmp_path / "access-2026-10-23T05-00-00.000.log")
    got = summarise(tmp_path / "access.log")
    assert got["totals"]["requests"] == 1
    assert got["files_read"] == 1


def test_list_log_files_orders_newest_first(tmp_path):
    main = tmp_path / "access.log"
    write_log(main, [line()])
    names = [
        "access-2026-10-21T01-00-00.000.log.gz",
        "access-2026-10-23T01-00-00.000.log.gz",
        "access-2026-10-22T01-00-00.000.log",
    ]
    for name in names:
        write_log(tmp_path / name, [line()])
    assert [p.name for p in ts.list_log_files(main)] == [
        "access.log",
        names[1],
        names[2],
        names[0],
    ]


def test_the_byte_cap_keeps_the_newest_lines_and_says_so(tmp_path):
    lines = [line(uri=f"/p{i}") for i in range(10)]
    write_log(tmp_path / "access.log", lines)
    each = len(lines[0]) + 1
    got = summarise(tmp_path / "access.log", byte_cap=int(each * 3.5))
    assert got["truncated_by_cap"] is True
    # The first (partial) line of the tail is dropped, leaving the newest three whole lines.
    assert {p["path"] for p in got["web_pages"]} == {"/p7", "/p8", "/p9"}
    assert got["totals"]["requests"] == 3


def test_the_byte_cap_is_not_a_truncation_when_the_window_start_was_reached(tmp_path):
    old = [line(uri=f"/old{i}", ts=NOW - 3600 * 48) for i in range(5)]
    new = [line(uri=f"/new{i}") for i in range(5)]
    write_log(tmp_path / "access.log", old + new)
    each = len(new[0]) + 1
    got = summarise(tmp_path / "access.log", byte_cap=int(each * 7.5))
    assert got["truncated_by_cap"] is False
    assert got["totals"]["requests"] == 5


def test_no_truncation_flag_when_everything_fits(tmp_path):
    write_log(tmp_path / "access.log", [line(), line()])
    got = summarise(tmp_path / "access.log")
    assert got["truncated_by_cap"] is False


def test_the_byte_cap_spans_files(tmp_path):
    write_log(tmp_path / "access.log", [line(uri="/main")])
    write_log(tmp_path / "access-2026-10-23T06-00-00.000.log", [line(uri="/rolled", ts=NOW - 7200)])
    got = summarise(tmp_path / "access.log", byte_cap=len(line()) + 5)
    assert got["truncated_by_cap"] is True
    assert {p["path"] for p in got["web_pages"]} == {"/main"}


def test_a_compressed_file_is_read_only_up_to_the_cap(tmp_path):
    write_log(tmp_path / "access.log", [line(uri="/main")])
    write_gz(
        tmp_path / "access-2026-10-23T06-00-00.000.log.gz",
        [line(uri=f"/g{i:02d}", ts=NOW - 7200) for i in range(20)],
    )
    each = len(line(uri="/g00")) + 1
    got = summarise(tmp_path / "access.log", byte_cap=each * 4)
    assert got["truncated_by_cap"] is True
    assert got["totals"]["requests"] == 4  # the main file's line, then three from the archive


def test_an_overlong_line_is_skipped(tmp_path):
    path = tmp_path / "access.log"
    huge = "x" * (ts.MAX_LINE_BYTES * 3)
    path.write_text("\n".join([line(uri="/before"), huge, line(uri="/after")]) + "\n")
    got = summarise(path)
    assert {p["path"] for p in got["web_pages"]} == {"/before", "/after"}


def test_hourly_series_covers_each_clock_hour_in_utc(tmp_path):
    write_log(
        tmp_path / "access.log",
        [
            line(ts=NOW - 2 * 3600 - 20 * 60 + 5),  # 05:50 -> bucket 05:00 (window starts 05:10)
            line(ts=NOW - 55 * 60),  # 07:15
            line(ts=NOW - 50 * 60, ua="curl/8.7.1"),  # 07:20, automated
            line(ts=NOW - 5 * 60),  # 08:05
        ],
    )
    got = summarise(tmp_path / "access.log", hours=3)
    hourly = got["hourly"]
    assert [h["hour"] for h in hourly] == [
        "2026-10-23T05:00:00Z",
        "2026-10-23T06:00:00Z",
        "2026-10-23T07:00:00Z",
        "2026-10-23T08:00:00Z",
    ]
    assert [h["requests"] for h in hourly] == [1, 0, 2, 1]
    assert [h["automated_requests"] for h in hourly] == [0, 0, 1, 0]
    assert sum(h["requests"] for h in hourly) == got["totals"]["requests"] == 4


def test_totals_kinds_statuses_and_countries(tmp_path):
    write_log(
        tmp_path / "access.log",
        [
            line(),
            line(ua="curl/8.7.1", status=404, country="CN", visitor_ip="192.0.2.4"),
            line(ua=GOOGLEBOT_PHONE, status=301, country="-", visitor_ip="192.0.2.5"),
            line(host="192.0.2.50", status=400, country="", visitor_ip="", remote_ip="192.0.2.6"),
            line(ua=CHROME, status=503, country="zz"),
            line(status=199, country="USA"),
        ],
    )
    got = summarise(tmp_path / "access.log")
    assert got["totals"]["requests"] == 6
    assert got["totals"]["automated_requests"] == 3
    assert got["totals"]["automated_share"] == 0.5
    kinds = {k["kind"]: k["requests"] for k in got["by_kind"]}
    assert list(kinds) == list(ts.KINDS)
    assert kinds["browser_like"] == 3 and kinds["scanner_or_tool"] == 1
    assert kinds["search_engine"] == 1 and kinds["direct_or_unknown_host"] == 1
    assert kinds["mcp_client"] == 0
    classes = {c["status_class"]: c["requests"] for c in got["by_status_class"]}
    assert classes == {"2xx": 1, "3xx": 1, "4xx": 2, "5xx": 1, "other": 1}
    countries = {c["country"]: c["requests"] for c in got["by_country"]}
    assert countries == {"US": 1, "CN": 1, "unknown": 3, "ZZ": 1}


def test_visitors_are_counted_ranked_and_masked(tmp_path):
    rows = [line(visitor_ip="203.0.113.9")] * 3
    rows += [line(visitor_ip="192.0.2.4", ua="curl/8.7.1", country="CN")] * 2
    rows += [line(country="", visitor_ip="", remote_ip="2001:db8:1:2::9")]
    write_log(tmp_path / "access.log", rows)
    got = summarise(tmp_path / "access.log")
    assert got["totals"]["distinct_visitors"] == 3
    top = got["top_visitors"]
    assert [v["address"] for v in top] == ["203.0.113.x", "192.0.2.x", "2001:db8:1:…"]
    assert [v["requests"] for v in top] == [3, 2, 1]
    assert [v["address_source"] for v in top] == ["header", "header", "remote_ip"]
    assert top[0]["kind"] == "browser_like" and top[1]["kind"] == "scanner_or_tool"
    assert top[1]["country"] == "CN"


def test_the_visitor_header_without_a_country_is_not_trusted(tmp_path):
    write_log(tmp_path / "access.log", [line(country="", visitor_ip="192.0.2.99")])
    top = summarise(tmp_path / "access.log")["top_visitors"]
    assert top[0]["address"] == "198.51.100.x"  # the connecting address, not the forgeable header
    assert top[0]["address_source"] == "remote_ip"


def test_top_visitors_are_limited_to_ten(tmp_path):
    write_log(tmp_path / "access.log", [line(visitor_ip=f"192.0.2.{i}") for i in range(1, 30)])
    got = summarise(tmp_path / "access.log")
    assert len(got["top_visitors"]) == 10
    assert got["totals"]["distinct_visitors"] == 29


def test_sites_never_show_an_address_literal(tmp_path):
    write_log(
        tmp_path / "access.log",
        [
            line(host="192.0.2.50"),
            line(host="192.0.2.50:443"),
            line(host="unrelated.invalid"),
            line(host="app.example.test"),
            line(host="mcp.example.test"),
        ],
    )
    sites = {s["host"]: s["requests"] for s in summarise(tmp_path / "access.log")["by_site"]}
    assert sites == {"(unknown host)": 3, "app.example.test": 1, "mcp.example.test": 1}


def test_by_site_groups_the_tail_into_other_hosts(tmp_path):
    rows = [line(host=f"h{i}.example.test") for i in range(25)]
    write_log(tmp_path / "access.log", rows)
    sites = summarise(tmp_path / "access.log", hosts=frozenset())["by_site"]
    assert len(sites) == 21 and sites[-1] == {"host": "(other host)", "requests": 5}


def test_user_agents_are_cut_to_eighty_characters(tmp_path):
    write_log(tmp_path / "access.log", [line(ua="Mozilla/5.0 " + "A" * 300)])
    got = summarise(tmp_path / "access.log")
    assert len(got["top_visitors"][0]["user_agent"]) == ts.UA_MAX
    write_log(tmp_path / "mcp.log", [line(host=MCP, ua="node" + "x" * 300, method="POST")])
    agents = summarise(tmp_path / "mcp.log")["mcp"]["top_user_agents"]
    assert all(len(a["user_agent"]) <= ts.UA_MAX for a in agents)


def test_mcp_section_lists_clients_and_paths_with_status(tmp_path):
    write_log(
        tmp_path / "access.log",
        [
            line(host=MCP, method="POST", uri="/", status=401, ua="mcpbeat/0.1"),
            line(host=MCP, method="POST", uri="/", status=401, ua="mcpbeat/0.1"),
            line(host=MCP, method="POST", uri="/", status=200, ua="node"),
            line(host=MCP, method="GET", uri="/.well-known/oauth-protected-resource", status=404),
            line(host="192.0.2.50", method="POST", uri="/", ua="mcpbeat/0.1"),  # not the MCP site
        ],
    )
    mcp = summarise(tmp_path / "access.log")["mcp"]
    assert mcp["top_user_agents"][0] == {
        "user_agent": "mcpbeat/0.1",
        "kind": "mcp_directory_or_probe",
        "requests": 2,
    }
    assert {"user_agent": "node", "kind": "mcp_client", "requests": 1} in mcp["top_user_agents"]
    assert mcp["top_paths"][0] == {"method": "POST", "path": "/", "status": 401, "requests": 2}
    assert len(mcp["top_paths"]) == 3


def test_web_pages_cover_only_people_like_and_search_traffic_on_the_web_site(tmp_path):
    write_log(
        tmp_path / "access.log",
        [
            line(uri="/pricing"),
            line(uri="/pricing"),
            line(uri="/pricing", ua=GOOGLEBOT_PHONE),
            line(uri="/pricing", ua="curl/8.7.1"),  # a tool
            line(host="api.example.test", uri="/health"),  # the API site
            line(host=MCP, uri="/"),  # the MCP site
            line(host="192.0.2.50", uri="/admin"),  # unknown host
            line(uri="/about", ua="Mozilla/5.0 (compatible; SomeBot/1.0)"),  # "other"
        ],
    )
    pages = summarise(tmp_path / "access.log")["web_pages"]
    assert pages == [{"path": "/pricing", "browser_like": 2, "search_engine": 1, "total": 3}]


def test_nothing_returned_holds_a_full_address_or_a_raw_line(tmp_path):
    write_log(
        tmp_path / "access.log",
        [
            line(),
            line(host="192.0.2.50", visitor_ip="203.0.113.77", remote_ip="198.51.100.88"),
            line(country="", visitor_ip="", remote_ip="2001:db8:1:2:3:4:5:6"),
            line(ua="<img src=x onerror=alert(1)>"),
        ],
    )
    text = json.dumps(summarise(tmp_path / "access.log"))
    for secret in (
        VISITOR,
        EDGE,
        "192.0.2.50",
        "203.0.113.77",
        "198.51.100.88",
        "2001:db8:1:2:3:4:5:6",
        "handled request",
        "http.log.access",
        "remote_port",
    ):
        assert secret not in text


# ---------------------------------------------------------------------------
# disabled modes
# ---------------------------------------------------------------------------


def _assert_disabled(got, reason):
    assert got["enabled"] is False and got["reason"] == reason
    assert got["totals"]["requests"] == 0 and got["files_read"] == 0
    assert [k["kind"] for k in got["by_kind"]] == list(ts.KINDS)
    assert got["hourly"] == [] and got["top_visitors"] == []


@pytest.mark.parametrize("path", ["", "  "])
def test_not_configured(path):
    _assert_disabled(summarise(path), "not_configured")


def test_missing_file_and_directory(tmp_path):
    _assert_disabled(summarise(tmp_path / "nope.log"), "file_missing")
    _assert_disabled(summarise(tmp_path), "file_missing")
    _assert_disabled(summarise(tmp_path / "nope" / "deeper.log"), "file_missing")


def test_unreadable_file(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can read a file with mode 000")
    path = tmp_path / "access.log"
    write_log(path, [line()])
    path.chmod(0)
    try:
        _assert_disabled(summarise(path), "unreadable")
    finally:
        path.chmod(0o600)


def test_an_empty_log_is_enabled_with_zero_requests(tmp_path):
    (tmp_path / "access.log").write_text("")
    got = summarise(tmp_path / "access.log")
    assert got["enabled"] is True and got["reason"] is None
    assert got["totals"] == {
        "requests": 0,
        "distinct_visitors": 0,
        "automated_requests": 0,
        "automated_share": 0.0,
    }
    assert len(got["hourly"]) == 25  # 08:00 yesterday .. 08:00 today, both partial


# ---------------------------------------------------------------------------
# get_traffic: cache and thread
# ---------------------------------------------------------------------------


def _settings(path, **over):
    base = get_settings().model_copy(
        update={
            "access_log_path": str(path),
            "traffic_site_hosts": "app.example.test,mcp.example.test",
            **over,
        }
    )
    return base


async def test_get_traffic_serves_a_cached_answer_until_it_expires(tmp_path, monkeypatch):
    import time

    path = tmp_path / "access.log"
    now = time.time()
    write_log(path, [line(ts=now - 10)])
    settings = _settings(path)
    clock = {"t": 1000.0}
    monkeypatch.setattr(ts, "_monotonic", lambda: clock["t"])

    first = await ts.get_traffic(settings, hours=1, exclude_ips_raw="")
    assert first["totals"]["requests"] == 1
    write_log(path, [line(ts=now - 10), line(ts=now - 5)])

    again = await ts.get_traffic(settings, hours=1, exclude_ips_raw="")
    assert again["totals"]["requests"] == 1  # cached
    # Different window / excludes are different cache keys
    assert (await ts.get_traffic(settings, hours=2, exclude_ips_raw=""))["totals"]["requests"] == 2
    assert (await ts.get_traffic(settings, hours=1, exclude_ips_raw=VISITOR))["totals"][
        "requests"
    ] == 0
    # Same excludes written differently share a key
    assert (await ts.get_traffic(settings, hours=1, exclude_ips_raw=f" {VISITOR} ,"))["totals"][
        "requests"
    ] == 0

    clock["t"] += ts.CACHE_TTL_SECONDS + 1
    assert (await ts.get_traffic(settings, hours=1, exclude_ips_raw=""))["totals"]["requests"] == 2


async def test_the_cache_is_bounded(tmp_path, monkeypatch):
    path = tmp_path / "access.log"
    write_log(path, [line(ts=NOW)])
    settings = _settings(path)
    monkeypatch.setattr(ts, "_monotonic", lambda: 5.0)
    for hours in range(1, ts.CACHE_MAX_ENTRIES + 10):
        await ts.get_traffic(settings, hours=hours, exclude_ips_raw="")
    assert len(ts._cache) <= ts.CACHE_MAX_ENTRIES


async def test_get_traffic_uses_the_configured_site_hosts(tmp_path):
    import time

    path = tmp_path / "access.log"
    write_log(path, [line(ts=time.time() - 5, host="other.example.test")])
    got = await ts.get_traffic(_settings(path), hours=1, exclude_ips_raw="")
    assert {k["kind"]: k["requests"] for k in got["by_kind"]}["direct_or_unknown_host"] == 1
    ts.cache_clear()
    got = await ts.get_traffic(_settings(path, traffic_site_hosts=""), hours=1, exclude_ips_raw="")
    assert {k["kind"]: k["requests"] for k in got["by_kind"]}["browser_like"] == 1


async def test_get_traffic_when_not_configured():
    settings = SimpleNamespace(access_log_path="", traffic_hosts=[])
    got = await ts.get_traffic(settings, hours=24, exclude_ips_raw="")
    assert got["enabled"] is False and got["reason"] == "not_configured"


def test_settings_parse_traffic_hosts():
    settings = get_settings().model_copy(
        update={"traffic_site_hosts": " App.Example.test, ,API.example.test,,"}
    )
    assert settings.traffic_hosts == ["app.example.test", "api.example.test"]
    assert get_settings().model_copy(update={"traffic_site_hosts": ""}).traffic_hosts == []
    assert get_settings().model_copy(update={"access_log_path": ""}).access_log_path == ""
