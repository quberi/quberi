"""Plain geometry and ordering for OCR results. No torch, no OpenCV: all of this is
testable without a model.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

from .base import OcrLine, Polygon


def fit_within(width: int, height: int, max_side: int) -> tuple[int, int]:
    """Size after shrinking so the longest side is at most ``max_side`` (never enlarges)."""

    longest = max(width, height)
    if longest <= max_side:
        return width, height
    scale = max_side / longest
    return max(1, round(width * scale)), max(1, round(height * scale))


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def upright_size(polygon: Polygon) -> tuple[int, int, bool]:
    """Size of the upright rectangle a four-corner text box should be warped into.

    Returns ``(width, height, rotate)``. ``rotate`` is true for a box much taller than
    it is wide (vertical writing, or a dimension note turned 90 degrees): the recogniser
    reads left to right, so those crops are turned a quarter turn first.
    """

    p0, p1, p2, p3 = polygon
    width = max(_dist(p0, p1), _dist(p3, p2))
    height = max(_dist(p0, p3), _dist(p1, p2))
    return max(1, round(width)), max(1, round(height)), height >= 1.5 * width


def order_polygon(points: Sequence[tuple[float, float]]) -> Polygon:
    """Put four corners in a fixed order: top-left, top-right, bottom-right, bottom-left."""

    if len(points) != 4:
        raise ValueError("a text box has exactly four corners")
    by_sum = sorted(points, key=lambda p: p[0] + p[1])
    by_diff = sorted(points, key=lambda p: p[1] - p[0])
    top_left, bottom_right = by_sum[0], by_sum[-1]
    top_right, bottom_left = by_diff[0], by_diff[-1]
    ordered = (top_left, top_right, bottom_right, bottom_left)
    # Degenerate input (for example all four points equal) can pick one corner twice.
    if len(set(ordered)) != 4:
        return (tuple(points[0]), tuple(points[1]), tuple(points[2]), tuple(points[3]))  # type: ignore[return-value]
    return ordered


def reading_order(lines: Sequence[OcrLine]) -> list[OcrLine]:
    """Top to bottom, and left to right within a row.

    Two lines share a row when the second one's vertical centre falls inside the row
    leader's height band. This keeps a table row together without merging two text
    lines that merely sit close above each other.
    """

    def centre_y(line: OcrLine) -> float:
        _, top, _, bottom = line.bbox
        return (top + bottom) / 2

    rows: list[list[OcrLine]] = []
    for line in sorted(lines, key=centre_y):
        if rows:
            leader_top, leader_bottom = rows[-1][0].bbox[1], rows[-1][0].bbox[3]
            if leader_top <= centre_y(line) <= leader_bottom:
                rows[-1].append(line)
                continue
        rows.append([line])
    ordered: list[OcrLine] = []
    for row in rows:
        ordered.extend(sorted(row, key=lambda ln: ln.bbox[0]))
    return ordered


# A part number, drawing number, dimension or model: has a digit, is short, and is not
# a plain sentence. Deliberately loose; this only decides which lines to keep first
# when a picture has more lines than the cap, never what counts as a match.
_HAS_DIGIT = re.compile(r"\d")
# Letters joined by hyphens or slashes with no spaces, such as QS-FLG-STD or GB/T:
# a drawing or standard code even without a digit.
_LETTER_CODE = re.compile(r"^[A-Za-z]{1,8}([-/_][A-Za-z0-9]{1,12})+$")
_CODE_MAX_LEN = 40
_CJK = re.compile(r"[\u4e00-\u9fff]")
# "法兰盘 外径 Φ520" (5 Chinese characters) is a labelled dimension; a sentence with a
# number in it is prose. More than this many Chinese characters means prose.
_CODE_MAX_CJK = 6


def looks_like_code(text: str) -> bool:
    stripped = text.strip()
    if not 0 < len(stripped) <= _CODE_MAX_LEN:
        return False
    if len(_CJK.findall(stripped)) > _CODE_MAX_CJK:
        return False
    return bool(_HAS_DIGIT.search(stripped) or _LETTER_CODE.match(stripped))


def cap_lines(lines: Sequence[OcrLine], max_lines: int) -> tuple[list[OcrLine], int]:
    """Keep at most ``max_lines`` lines, choosing the ones most likely to matter.

    Lines are ranked, not cut in the order they were found, because the first 500 in
    reading order can lose exactly the part number somebody will search for. Order of
    preference: code-like lines first, then larger text (title blocks, labels), then
    higher confidence. The survivors are returned in reading order.

    The ranking is a starting point to be tuned on real drawings.
    """

    if max_lines <= 0:
        return [], len(lines)
    if len(lines) <= max_lines:
        return list(lines), 0

    def rank(line: OcrLine) -> tuple[int, float, float]:
        _, top, _, bottom = line.bbox
        return (1 if looks_like_code(line.text) else 0, bottom - top, line.confidence)

    kept = sorted(lines, key=rank, reverse=True)[:max_lines]
    return reading_order(kept), len(lines) - max_lines
