"""The platform admin message catalog: its integrity, rendering and language choice.

Every ``admin.*`` code the source raises must exist in the catalog, and every catalog code
must be used, so the console's dictionary (which has one entry per catalog code) cannot
drift from what the API can send.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from quberi_api.api.admin_errors import AdminApiException, from_coded
from quberi_api.services import platform_admin as svc
from quberi_api.services import traffic_summary
from quberi_api.services.admin_messages import (
    CATALOG,
    CodedValueError,
    pick_language,
    ref,
    render,
)
from quberi_api.services.oauth import clients

SRC = Path(__file__).resolve().parents[1] / "src" / "quberi_api"
SCANNED = [
    SRC / "api" / "security.py",
    *sorted((SRC / "api" / "v1" / "admin").glob("*.py")),
    SRC / "services" / "platform_admin.py",
    SRC / "services" / "oauth" / "clients.py",
    SRC / "services" / "ai_providers.py",
]
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_CODE = re.compile(r"^admin\.[a-z_.]+$")
_CJK = re.compile(r"[\u4e00-\u9fff]")


# ---------------------------------------------------------------------------
# Catalog integrity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(CATALOG))
def test_every_entry_has_english_and_chinese_text(code):
    message = CATALOG[code]
    assert _CODE.match(code)
    assert message.en.strip() and message.zh.strip()
    assert _CJK.search(message.zh), "the Chinese text must contain Chinese"


@pytest.mark.parametrize("code", sorted(CATALOG))
def test_both_languages_use_the_same_placeholders(code):
    message = CATALOG[code]
    assert set(_PLACEHOLDER.findall(message.en)) == set(_PLACEHOLDER.findall(message.zh))


def _used_codes() -> set[str]:
    used: set[str] = set()
    for path in SCANNED:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _CODE.match(node.value)
            ):
                used.add(node.value)
    return used


def test_every_code_used_in_the_source_is_in_the_catalog_and_none_is_dead():
    used = _used_codes()
    assert used - set(CATALOG) == set(), "codes used but missing from the catalog"
    assert set(CATALOG) - used == set(), "catalog codes that nothing uses"


def test_the_traffic_note_matches_the_text_the_summary_service_carries():
    assert CATALOG["admin.traffic.address_note"].en == traffic_summary.ADDRESS_NOTE


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def test_render_fills_placeholders_in_the_chosen_language():
    assert (
        render("admin.auth.password_too_short", "en", {"min": 12})
        == "password must be at least 12 characters"
    )
    assert render("admin.auth.password_too_short", "zh", {"min": 12}) == "密码至少需要 12 个字符"


def test_render_defaults_to_english_and_treats_other_languages_as_english():
    assert render("admin.oauth.tool_not_found") == "no such tool"
    assert render("admin.oauth.tool_not_found", "fr") == "no such tool"
    assert render("admin.oauth.tool_not_found", "zh") != "no such tool"


def test_a_placeholder_without_a_value_is_left_as_written():
    assert render("admin.orgs.not_found", "en", {}) == "organization not found: {id}"
    assert render("admin.orgs.not_found", "en", {"other": "x"}) == "organization not found: {id}"


def test_a_value_is_never_searched_for_more_placeholders():
    # ``{id}`` is the literal placeholder these OAuth messages talk about.
    text = render(
        "admin.oauth.url_placeholder_not_allowed",
        "en",
        {"subject": "the client id URL", "placeholder": "{id}"},
    )
    assert text == "the client id URL may not contain {id}"
    # a value that looks like another placeholder is not expanded
    assert (
        render("admin.orgs.not_found", "en", {"id": "{subject}", "subject": "x"})
        == "organization not found: {subject}"
    )


def test_a_message_reference_is_rendered_in_the_same_language():
    params = {"subject": ref("admin.oauth.subject.redirect", rule="'x'")}
    assert render("admin.oauth.url_port", "en", params) == "redirect 'x' may not name a port"
    assert render("admin.oauth.url_port", "zh", params) == "回调地址 'x' 不能指定端口"
    plain = {"subject": ref("admin.oauth.subject.client_id_url")}
    assert render("admin.oauth.url_port", "en", plain) == "the client id URL may not name a port"


def test_an_unknown_code_is_a_programming_error():
    with pytest.raises(KeyError):
        render("admin.nothing.here")


def test_numbers_are_rendered_as_text():
    assert render("admin.auth.password_too_long", "en", {"max": 128}).endswith("128 characters")


# ---------------------------------------------------------------------------
# pick_language
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header, expected",
    [
        (None, "en"),
        ("", "en"),
        ("zh-CN", "zh"),
        ("zh", "zh"),
        ("ZH-hans-CN", "zh"),
        ("zh-CN,zh;q=0.9,en;q=0.8", "zh"),
        ("en-US,en;q=0.9", "en"),
        ("en-US,en;q=0.9,zh;q=0.8", "en"),
        ("fr-FR", "en"),
        ("fr-FR, zh;q=0.5", "zh"),
        ("*", "en"),
        ("en;q=0.2, zh;q=0.9", "zh"),
        ("zh;q=0", "en"),
        ("zh;q=abc", "en"),
        (";;;,,,", "en"),
        ("\x00\x01 garbage", "en"),
    ],
)
def test_pick_language(header, expected):
    assert pick_language(header) == expected


def test_pick_language_reads_only_the_start_of_a_huge_header():
    assert pick_language("fr," * 1000 + "zh") == "en"
    assert pick_language("zh," + "fr," * 1000) == "zh"


def test_pick_language_ignores_things_that_are_not_text():
    assert pick_language(123) == "en"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The exceptions
# ---------------------------------------------------------------------------


def test_coded_value_error_is_a_value_error_with_the_english_text():
    exc = CodedValueError("admin.models.org_not_found", id="abc")
    assert isinstance(exc, ValueError)
    assert str(exc) == "organization abc not found"
    assert exc.code == "admin.models.org_not_found" and exc.params == {"id": "abc"}


def test_admin_api_exception_keeps_the_english_detail_and_headers():
    exc = AdminApiException(
        401, "admin.auth.invalid_token", headers={"WWW-Authenticate": "Bearer"}
    )
    assert exc.status_code == 401
    assert exc.detail == "invalid platform admin token"
    assert exc.headers == {"WWW-Authenticate": "Bearer"}
    assert exc.code == "admin.auth.invalid_token" and exc.params == {}


def test_from_coded_carries_code_and_params():
    exc = from_coded(CodedValueError("admin.orgs.not_found", id="7"), 404)
    assert (exc.status_code, exc.code, exc.params) == (404, "admin.orgs.not_found", {"id": "7"})
    assert exc.detail == "organization not found: 7"


# ---------------------------------------------------------------------------
# English text is exactly what the API said before codes existed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "password, username, expected",
    [
        ("short", "root", "password must be at least 12 characters"),
        ("x" * 129, "root", "password must be at most 128 characters"),
        (" leading spaces here", "root", "password must not start or end with spaces"),
        ("trailing spaces here ", "root", "password must not start or end with spaces"),
        ("aaaaaaaaaaaaaaaa", "root", "password is too repetitive"),
        ("my root passphrase!", "root", "password must not contain the username"),
    ],
)
def test_password_rules_keep_their_english_text(password, username, expected):
    with pytest.raises(svc.PlatformAdminError) as caught:
        svc.validate_new_password(password, username=username)
    assert caught.value.message == expected
    assert caught.value.status == 400 and caught.value.code in CATALOG


LEGACY_CLIENT_CONFIG_ERRORS = [
    ("my tool", "registered", ["loopback"],
     "a registered client id is 1 to 64 letters, digits, dots, dashes or underscores"),
    ("my-tool", "nonsense", ["loopback"], "unknown kind"),
    ("my-tool", "registered", [], "at least one redirect rule is required"),
    ("https://app.example.test/" + "x" * 300, "metadata_url", ["loopback"],
     "the client id URL is too long"),
    ("https://*.example.test/c.json", "metadata_url", ["loopback"],
     "the client id URL may not contain wildcards or spaces"),
    ("my-tool", "registered", ["https://evil.example.test/cb/{id}"],
     "redirect 'https://evil.example.test/cb/{id}' uses {id} but the client id does not"),
    ("https://app.example.test/{id}/{id}.json", "metadata_url", ["loopback"],
     "the client id URL may contain {id} at most once"),
    ("https://app.example.test/{x}/c.json", "metadata_url", ["loopback"],
     "the client id URL contains braces other than {id}"),
    ("https://app.example.test:99999/c.json", "metadata_url", ["loopback"],
     "the client id URL is not a valid address"),
    ("http://app.example.test/c.json", "metadata_url", ["loopback"],
     "the client id URL must be an https:// address"),
    ("https://u@app.example.test/c.json", "metadata_url", ["loopback"],
     "the client id URL may not carry credentials, a query or a fragment"),
    ("https://app.example.test:8443/c.json", "metadata_url", ["loopback"],
     "the client id URL may not name a port"),
    ("https://203.0.113.9/c.json", "metadata_url", ["loopback"],
     "the client id URL must use a real domain name, not an IP address"),
    ("https://{id}.example.test/c.json", "metadata_url", ["loopback"],
     "{id} may only appear in the path of the client id URL"),
    ("my-tool", "registered", ["http://evil.example.test/cb"],
     "redirect 'http://evil.example.test/cb' must be an https:// address"),
    ("my-tool", "registered", ["https://evil.example.test:444/cb"],
     "redirect 'https://evil.example.test:444/cb' may not name a port"),
]


@pytest.mark.parametrize("client_id, kind, redirects, expected", LEGACY_CLIENT_CONFIG_ERRORS)
def test_client_config_errors_keep_their_english_text(client_id, kind, redirects, expected):
    with pytest.raises(clients.ClientConfigError) as caught:
        clients.validate_client_config(client_id, kind, redirects)
    assert str(caught.value) == expected
    assert caught.value.code in CATALOG


def test_a_placeholder_not_allowed_message_names_what_it_cannot_contain():
    with pytest.raises(clients.ClientConfigError) as caught:
        clients._check_https_url(
            "https://app.example.test/{id}", subject=ref("admin.oauth.subject.client_id_url"), allow_id=False
        )
    assert str(caught.value) == "the client id URL may not contain {id}"
    assert caught.value.code == "admin.oauth.url_placeholder_not_allowed"
    assert caught.value.params["placeholder"] == "{id}"
