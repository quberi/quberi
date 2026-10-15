"""Spot likely sensitive data in text, and apply the user's edits to it.

Used by the "review before sending to the model" step: the text that is about
to leave for a model vendor is scanned, suspicious spans are highlighted, and
the person can delete or replace them.

This is a *heuristic aid*, not a data-loss-prevention guarantee. It finds
well-formed phone numbers, ID numbers, card numbers, e-mail addresses, keys and
the like. It cannot recognise names, addresses, salaries or anything that has no
fixed shape, and a miss is always possible. The UI says so.

Spans are offsets into the exact text that was scanned, so the screen can
highlight them. When the person edits the text, it is scanned again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Keep a single scan bounded, however large the text.
MAX_SPANS_PER_TEXT = 200


@dataclass(frozen=True)
class SensitiveSpan:
    start: int
    end: int
    kind: str


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

# Digits are matched with look-arounds instead of \b, because word boundaries
# do not exist between Chinese characters and digits.
_NOT_DIGIT_BEFORE = r"(?<![0-9])"
_NOT_DIGIT_AFTER = r"(?![0-9])"

_PHONE = re.compile(_NOT_DIGIT_BEFORE + r"1[3-9][0-9]{9}" + _NOT_DIGIT_AFTER)
_ID_CARD = re.compile(
    _NOT_DIGIT_BEFORE
    + r"[1-9][0-9]{5}(?:19|20)[0-9]{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12][0-9]|3[01])"
    + r"[0-9]{3}[0-9Xx](?![0-9Xx])"
)
_CARD = re.compile(_NOT_DIGIT_BEFORE + r"(?:[0-9][ -]?){15,18}[0-9]" + _NOT_DIGIT_AFTER)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_SECRETS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),  # OpenAI-style keys
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),  # Google API key
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),  # GitHub tokens
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),  # Slack tokens
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
    ),
]
# "password: hunter2" / "密码：xxxx" - only the value is flagged.
_PASSWORD = re.compile(
    r"(?i)(?:password|passwd|pwd|secret|token|密码|口令|密钥)\s*[:=：]\s*([^\s,;，；。]{3,})"
)
_INTERNAL_IP = re.compile(
    r"(?<![0-9.])(?:10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}"
    r"|192\.168\.[0-9]{1,3}\.[0-9]{1,3}"
    r"|172\.(?:1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3})(?![0-9])"
)

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"


def _id_card_valid(value: str) -> bool:
    total = sum(int(d) * w for d, w in zip(value[:17], _ID_WEIGHTS))
    return _ID_CHECK[total % 11] == value[17].upper()


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# When two findings overlap, the one earlier in this list wins.
_PRIORITY = ["secret", "id_card", "bank_card", "phone", "email", "password", "internal_ip"]


def find_sensitive(text: str) -> list[SensitiveSpan]:
    """Non-overlapping suspected-sensitive spans in ``text``, in reading order."""

    if not text:
        return []
    found: list[SensitiveSpan] = []

    for pattern in _SECRETS:
        for m in pattern.finditer(text):
            found.append(SensitiveSpan(m.start(), m.end(), "secret"))
    for m in _ID_CARD.finditer(text):
        if _id_card_valid(m.group(0)):
            found.append(SensitiveSpan(m.start(), m.end(), "id_card"))
    for m in _CARD.finditer(text):
        digits = re.sub(r"[ -]", "", m.group(0))
        if 16 <= len(digits) <= 19 and _luhn_valid(digits):
            found.append(SensitiveSpan(m.start(), m.end(), "bank_card"))
    for m in _PHONE.finditer(text):
        found.append(SensitiveSpan(m.start(), m.end(), "phone"))
    for m in _EMAIL.finditer(text):
        found.append(SensitiveSpan(m.start(), m.end(), "email"))
    for m in _PASSWORD.finditer(text):
        found.append(SensitiveSpan(m.start(1), m.end(1), "password"))
    for m in _INTERNAL_IP.finditer(text):
        found.append(SensitiveSpan(m.start(), m.end(), "internal_ip"))

    # Resolve overlaps: higher-priority kind first, then the longer span.
    found.sort(key=lambda s: (_PRIORITY.index(s.kind), -(s.end - s.start), s.start))
    kept: list[SensitiveSpan] = []
    for span in found:
        if any(span.start < k.end and k.start < span.end for k in kept):
            continue
        kept.append(span)
    kept.sort(key=lambda s: s.start)
    return kept[:MAX_SPANS_PER_TEXT]
