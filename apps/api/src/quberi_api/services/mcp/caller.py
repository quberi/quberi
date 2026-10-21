"""Who is calling, in a few words a person can recognise.

An MCP client introduces itself in its ``initialize`` request (``clientInfo``: a name, an
optional display title and a version). That is what lets a person looking at their list of
connected tools tell "Codex on this laptop" from "Claude" when the registered tool name is
the same for both. It is only what the program says about itself, so it is shown, never
trusted: it is cleaned, capped, and used for display and nothing else.
"""

from __future__ import annotations

import re
from typing import Any, Optional

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_NAME_MAX = 60
_VERSION_MAX = 30
_AGENT_MAX = 80
LABEL_MAX = 120


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return _CONTROL.sub(" ", value).strip()[:limit].strip()


def caller_label(params: dict, user_agent: Optional[str] = None) -> Optional[str]:
    """``"codex-mcp-client 0.52.0"`` from an ``initialize`` request, or ``None``.

    The display title wins over the technical name. When the client sent neither, the first
    words of its User-Agent are used instead, which at least say what kind of program it is.
    """

    info = params.get("clientInfo") if isinstance(params, dict) else None
    if isinstance(info, dict):
        name = _clean(info.get("title"), _NAME_MAX) or _clean(info.get("name"), _NAME_MAX)
        version = _clean(info.get("version"), _VERSION_MAX)
        if name:
            return f"{name} {version}".strip()[:LABEL_MAX]
    agent = _clean(user_agent, _AGENT_MAX)
    return agent or None
