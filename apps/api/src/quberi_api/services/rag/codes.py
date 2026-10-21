"""Part numbers, drawing numbers and dimensions: matching them exactly.

Search by meaning is good at "bearing" finding "deep-groove ball bearing" and poor at
``Φ520`` versus ``Φ522``: those two are almost the same point in meaning space, and for
the person searching, one digit is the whole question. So a line that looks like a code is
also filed as an exact key, and a query that contains a code is also looked up by key.

The key is **forgiving in one narrow way**. Text read out of a drawing confuses a few
shapes (a diameter sign read as the Chinese character 中), and people type the diameter
sign several ways. Both sides go through the same function, so ``Φ520``, ``Ø520``,
``中520`` and ``ф520`` give one key. It never folds a digit into a letter or the reverse:
``Q235B`` and ``Q2358`` stay different, because the number somebody is looking for must
not be answered with a different number.

Finding codes is done word by word: split the text into candidates, then ask of each
whether it looks like a code. One large pattern run over the whole text was tried first
and its alternatives took matches from each other.
"""

from __future__ import annotations

import re
import unicodedata

# A diameter sign, typed or misread. 中 is what the recogniser most often returns for it.
_DIAMETER_LIKE = "ΦϕφфФ∅Øø⌀中"
_DIAMETER_KEY = "Φ"

MIN_KEY_LEN = 3
MAX_KEYS_PER_TEXT = 40
MAX_KEYS_PER_PICTURE = 200

# Prefixes that are standards or part families written as two words: "GB/T 276-2013",
# "DIN 912". The number after them belongs to them.
_STANDARD_PREFIX = re.compile(r"^[A-Z]{2,5}(?:/[A-Z])?$")

_DIAMETER_WORD = re.compile(rf"^[{_DIAMETER_LIKE}]\d[\d.]*[A-Za-z]{{0,2}}\d{{0,2}}$")
# Letters then a digit, with more after: Q235B, M12, R0.5, HZ8800B, 6205-2RS parts.
_LETTERS_DIGITS = re.compile(r"^[A-Za-z]{1,4}\d[\w.\-/]*$")
# Digits first, joined by separators: 20261019-0042, 6205-2RS, 520-0.02.
_DIGITS_JOINED = re.compile(r"^\d{2,}[A-Za-z]{0,3}(?:[-/_.]\d*[A-Za-z0-9]*)+$")
# A long plain number: 6205, 8800B.
_LONG_NUMBER = re.compile(r"^\d{3,}[A-Za-z]{0,3}$")
# Capital letters joined by hyphens or slashes, no digit needed: QS-FLG-STD, GB/T.
_CAPS_JOINED = re.compile(r"^[A-Z]{2,8}(?:[-/_][A-Z0-9]{1,8}){1,5}$")
# A word that is a code only because of what it is made of; splits text on anything that
# is neither a letter, digit, nor one of the joiners.
_CANDIDATE = re.compile(rf"[A-Za-z0-9{_DIAMETER_LIKE}][A-Za-z0-9{_DIAMETER_LIKE}.\-/_]*")


def code_key(token: str) -> str:
    """The forgiving form of one code, used on both sides of a match."""

    folded = unicodedata.normalize("NFKC", token)
    folded = "".join(_DIAMETER_KEY if ch in _DIAMETER_LIKE else ch for ch in folded)
    return re.sub(r"\s+", "", folded).upper()


def _trim(word: str) -> str:
    return word.strip(".-/_")


def _is_code(word: str) -> bool:
    if not word or len(word) < MIN_KEY_LEN:
        return False
    # Case matters here: "WELL-KNOWN" is shouted prose, "well-known" is prose; only
    # capitals joined by separators count as a code without a digit.
    if _CAPS_JOINED.match(word):
        return True
    return bool(
        _DIAMETER_WORD.match(word)
        or _LETTERS_DIGITS.match(word)
        or _DIGITS_JOINED.match(word)
        or _LONG_NUMBER.match(word)
    )


def extract_codes(text: str) -> list[str]:
    """Code keys found in a line or a query, in order, without repeats.

    Chinese characters (other than the diameter-like 中) end a word, so ``法兰外径 Φ520``
    yields ``Φ520`` and not the label.
    """

    normalised = unicodedata.normalize("NFKC", text or "")
    # A diameter sign read as 中 inside a Chinese sentence: 外径中520 -> 外径 中520.
    normalised = re.sub(r"(?<=[\u4e00-\u9fff])中(?=\d)", " 中", normalised)

    words = [_trim(m.group(0)) for m in _CANDIDATE.finditer(normalised)]
    seen: dict[str, None] = {}
    i = 0
    while i < len(words):
        word = words[i]
        # "GB/T" or "DIN" followed by its number is one code, not two.
        if (
            _STANDARD_PREFIX.match(word)
            and i + 1 < len(words)
            and re.match(r"^\d{2,}(?:[-.]\d+)*$", words[i + 1])
            and _is_adjacent(normalised, word, words[i + 1])
        ):
            seen.setdefault(code_key(f"{word}{words[i + 1]}"), None)
            i += 2
            continue
        if _is_code(word):
            seen.setdefault(code_key(word), None)
        if len(seen) >= MAX_KEYS_PER_TEXT:
            break
        i += 1
    return list(seen)


def _is_adjacent(text: str, first: str, second: str) -> bool:
    """Whether ``second`` follows ``first`` with only a space or two between them."""

    return re.search(re.escape(first) + r"[ ]{1,2}" + re.escape(second), text) is not None


def codes_in_lines(lines: list[str]) -> list[str]:
    """All distinct code keys across the lines of one picture, capped."""

    seen: dict[str, None] = {}
    for line in lines:
        for key in extract_codes(line):
            seen.setdefault(key, None)
        if len(seen) >= MAX_KEYS_PER_PICTURE:
            break
    return list(seen)[:MAX_KEYS_PER_PICTURE]
