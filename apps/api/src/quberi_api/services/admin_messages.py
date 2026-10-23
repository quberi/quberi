"""Stable message codes for the platform admin console.

Every human-readable message the platform admin API can send to the console is
registered here under a stable ``admin.*`` code, with an English and a Simplified
Chinese template. An error answers with the code (and its parameters) so the console
can show the text from its own dictionary; ``detail`` still carries the text, in the
language the request asked for with ``Accept-Language`` (English when it asks for
none), so scripts and ``curl`` keep working.

The English text of each entry is exactly what the API said before codes existed.

Templates use ``{name}`` placeholders, filled in a single pass: a value is never
searched for further placeholders, so a literal ``{id}`` passed as a value survives. A
placeholder with no value is left as written. A value may itself be a *message
reference* (see :func:`ref`), which is rendered in the same language first; this is how
one sentence subject ("the client id URL") serves many sentences.

This module deliberately imports nothing from FastAPI, so services can raise
:class:`CodedValueError` without depending on the web layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional

Lang = Literal["en", "zh"]
DEFAULT_LANG: Lang = "en"


@dataclass(frozen=True)
class Message:
    en: str
    zh: str


# One code per line (the key and ``Message(`` on the same line): the web console's tests
# read the codes out of this file with a regular expression.
CATALOG: dict[str, Message] = {
    "admin.auth.invalid_token": Message(
        en="invalid platform admin token",
        zh="平台管理员令牌无效",
    ),
    "admin.auth.sign_in_required": Message(
        en="sign in required",
        zh="请先登录",
    ),
    "admin.auth.password_too_short": Message(
        en="password must be at least {min} characters",
        zh="密码至少需要 {min} 个字符",
    ),
    "admin.auth.password_too_long": Message(
        en="password must be at most {max} characters",
        zh="密码最多 {max} 个字符",
    ),
    "admin.auth.password_edge_spaces": Message(
        en="password must not start or end with spaces",
        zh="密码开头和结尾不能有空格",
    ),
    "admin.auth.password_repetitive": Message(
        en="password is too repetitive",
        zh="密码过于重复",
    ),
    "admin.auth.password_has_username": Message(
        en="password must not contain the username",
        zh="密码不能包含用户名",
    ),
    "admin.auth.try_again_shortly": Message(
        en="try again shortly",
        zh="请稍后重试",
    ),
    "admin.auth.too_many_attempts": Message(
        en="too many attempts, try again later",
        zh="尝试次数过多，请稍后再试",
    ),
    "admin.auth.bad_credentials": Message(
        en="invalid username or password",
        zh="用户名或密码不正确",
    ),
    "admin.auth.current_password_wrong": Message(
        en="current password is incorrect",
        zh="当前密码不正确",
    ),
    "admin.auth.password_unchanged": Message(
        en="choose a password different from the current one",
        zh="请选择与当前密码不同的新密码",
    ),
    "admin.oauth.client_id_taken": Message(
        en="a tool with that client id is already configured",
        zh="已配置了使用该客户端 ID 的外部工具",
    ),
    "admin.oauth.tool_not_found": Message(
        en="no such tool",
        zh="找不到该外部工具",
    ),
    "admin.oauth.unknown_kind": Message(
        en="unknown kind",
        zh="未知的类型",
    ),
    "admin.oauth.registered_id_format": Message(
        en="a registered client id is 1 to 64 letters, digits, dots, dashes or underscores",
        zh="已注册的客户端 ID 由 1 到 64 个字母、数字、点、短横线或下划线组成",
    ),
    "admin.oauth.url_too_long": Message(
        en="{subject} is too long",
        zh="{subject} 过长",
    ),
    "admin.oauth.url_wildcard_space": Message(
        en="{subject} may not contain wildcards or spaces",
        zh="{subject} 不能包含通配符或空格",
    ),
    "admin.oauth.url_placeholder_not_allowed": Message(
        en="{subject} may not contain {placeholder}",
        zh="{subject} 不能包含 {placeholder}",
    ),
    "admin.oauth.url_placeholder_once": Message(
        en="{subject} may contain {placeholder} at most once",
        zh="{subject} 中的 {placeholder} 最多出现一次",
    ),
    "admin.oauth.url_braces": Message(
        en="{subject} contains braces other than {placeholder}",
        zh="{subject} 包含 {placeholder} 以外的花括号",
    ),
    "admin.oauth.url_invalid": Message(
        en="{subject} is not a valid address",
        zh="{subject} 不是有效的地址",
    ),
    "admin.oauth.url_not_https": Message(
        en="{subject} must be an https:// address",
        zh="{subject} 必须是 https:// 地址",
    ),
    "admin.oauth.url_extras": Message(
        en="{subject} may not carry credentials, a query or a fragment",
        zh="{subject} 不能包含账号凭据、查询参数或片段",
    ),
    "admin.oauth.url_port": Message(
        en="{subject} may not name a port",
        zh="{subject} 不能指定端口",
    ),
    "admin.oauth.url_domain": Message(
        en="{subject} must use a real domain name, not an IP address",
        zh="{subject} 必须使用真实域名，不能使用 IP 地址",
    ),
    "admin.oauth.url_placeholder_path": Message(
        en="{placeholder} may only appear in the path of {subject}",
        zh="{placeholder} 只能出现在 {subject} 的路径中",
    ),
    "admin.oauth.redirect_needs_id": Message(
        en="redirect {rule} uses {placeholder} but the client id does not",
        zh="回调地址 {rule} 使用了 {placeholder}，但客户端 ID 中没有",
    ),
    "admin.oauth.redirect_required": Message(
        en="at least one redirect rule is required",
        zh="至少需要一条回调规则",
    ),
    "admin.oauth.subject.client_id_url": Message(
        en="the client id URL",
        zh="客户端 ID 网址",
    ),
    "admin.oauth.subject.redirect": Message(
        en="redirect {rule}",
        zh="回调地址 {rule}",
    ),
    "admin.models.binding_not_found": Message(
        en="platform binding not found",
        zh="找不到该平台密钥",
    ),
    "admin.models.visibility_mode_invalid": Message(
        en="invalid platform_model_visibility mode {mode}",
        zh="平台模型可见性模式 {mode} 无效",
    ),
    "admin.models.org_not_found": Message(
        en="organization {id} not found",
        zh="找不到组织 {id}",
    ),
    "admin.orgs.not_found": Message(
        en="organization not found: {id}",
        zh="找不到组织：{id}",
    ),
    "admin.traffic.address_note": Message(
        en=(
            "Addresses are masked. A visitor address taken from a request header can be forged by "
            "anyone who reaches the server directly, without going through the CDN; rows marked "
            "'remote_ip' use the connecting address."
        ),
        zh=(
            "地址已做脱敏处理。取自请求头的访客地址，可被绕过 CDN、直接访问服务器的人伪造；"
            "标记为 'remote_ip' 的行使用的是连接地址。"
        ),
    ),
}

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_MAX_ACCEPT_LANGUAGE = 500
_MAX_TAGS = 20


def pick_language(accept_language: Optional[str]) -> Lang:
    """The catalog language an ``Accept-Language`` header asks for.

    The acceptable tag with the highest quality whose primary subtag is ``zh`` or ``en``
    wins (the first one on a tie); anything else, including no header or a garbled one,
    is English. Never raises.
    """

    if not accept_language or not isinstance(accept_language, str):
        return DEFAULT_LANG
    best: Optional[Lang] = None
    best_q = 0.0
    for part in accept_language[:_MAX_ACCEPT_LANGUAGE].split(",")[:_MAX_TAGS]:
        tag, _, rest = part.partition(";")
        primary = tag.strip().lower().split("-")[0]
        if primary not in ("zh", "en"):
            continue
        q = 1.0
        rest = rest.strip().lower()
        if rest.startswith("q="):
            try:
                q = float(rest[2:].strip())
            except ValueError:
                continue
        if q > best_q:
            best_q = q
            best = "zh" if primary == "zh" else "en"
    return best or DEFAULT_LANG


def ref(code: str, **params: Any) -> dict[str, Any]:
    """A message reference: a parameter value that is itself a catalog message."""

    return {"code": code, "params": params} if params else {"code": code}


def _is_ref(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("code"), str)


def render(
    code: str, lang: str = DEFAULT_LANG, params: Optional[Mapping[str, Any]] = None
) -> str:
    """The text of ``code`` in ``lang`` (English unless ``zh``) with ``params`` filled in.

    Raises ``KeyError`` for a code that is not in the catalog (a programming error).
    """

    message = CATALOG[code]
    template = message.zh if lang == "zh" else message.en
    values = params or {}

    def fill(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name not in values:
            return match.group(0)
        value = values[name]
        if _is_ref(value):
            return render(value["code"], lang, value.get("params"))
        return str(value)

    return _PLACEHOLDER.sub(fill, template)


class CodedValueError(ValueError):
    """A ``ValueError`` that carries a catalog ``code`` and its ``params``.

    ``str(exc)`` is the English text, so existing ``except ValueError`` handlers that
    show ``str(exc)`` behave exactly as before.
    """

    def __init__(self, code: str, **params: Any) -> None:
        super().__init__(render(code, DEFAULT_LANG, params))
        self.code = code
        self.params = params
