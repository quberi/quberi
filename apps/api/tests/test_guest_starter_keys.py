"""The guest chat's suggested questions are named in two places that must agree.

The server owns the keys (popularity counting rejects unknown ones); the web
app owns the wording, in both languages. A key on one side only is either a
suggestion that can never be counted, or one with no text.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from quberi_api.services.demo_content import GUEST_STARTER_KEYS
from quberi_api.services.guest_starters import is_valid_key

WEB = Path(__file__).resolve().parents[2] / "web" / "src"


def _web_keys() -> list[str]:
    source = (WEB / "features" / "chat" / "starters.ts").read_text(encoding="utf-8")
    block = re.search(r"GUEST_STARTER_KEYS = \[(.*?)\] as const", source, re.S)
    assert block, "could not find GUEST_STARTER_KEYS in starters.ts"
    return re.findall(r"'([a-z_]+)'", block.group(1))


def test_server_and_web_offer_the_same_questions_in_the_same_order() -> None:
    assert list(GUEST_STARTER_KEYS) == _web_keys()


def test_keys_are_unique_and_short_enough_to_store() -> None:
    assert len(set(GUEST_STARTER_KEYS)) == len(GUEST_STARTER_KEYS)
    assert all(0 < len(k) <= 32 for k in GUEST_STARTER_KEYS)


def test_every_key_has_text_in_both_languages_and_no_text_is_orphaned() -> None:
    for locale in ("zh-CN", "en-US"):
        data = json.loads((WEB / "i18n" / "locales" / f"{locale}.json").read_text(encoding="utf-8"))
        labels = data["chat"]["guest"]["starters"]
        assert sorted(labels) == sorted(GUEST_STARTER_KEYS), locale
        assert all(isinstance(v, str) and v.strip() for v in labels.values()), locale


def test_the_security_questions_the_banner_promises_are_offered() -> None:
    for key in ("visibility", "offboard", "connect", "audit"):
        assert is_valid_key(key)
    assert not is_valid_key("not-a-question")
