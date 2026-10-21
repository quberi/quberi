"""OCR geometry and ordering: pure logic, no model."""

from __future__ import annotations

import pytest

from quberi_api.services.ai.ocr.base import OcrLine
from quberi_api.services.ai.ocr.geometry import (
    cap_lines,
    fit_within,
    looks_like_code,
    order_polygon,
    reading_order,
    upright_size,
)
from quberi_api.services.ai.ocr.models import HARD_MAX_DET_SIDE, EngineConfig


def _line(text, x0, y0, x1, y1, conf=0.9):
    return OcrLine(text, conf, ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))


def test_fit_within_shrinks_the_long_side_and_never_enlarges():
    assert fit_within(4000, 2000, 2000) == (2000, 1000)
    assert fit_within(1000, 800, 2000) == (1000, 800)
    assert fit_within(1, 5000, 100) == (1, 100)


def test_upright_size_flags_tall_boxes_for_a_quarter_turn():
    wide = ((0, 0), (200, 0), (200, 30), (0, 30))
    tall = ((0, 0), (30, 0), (30, 200), (0, 200))
    assert upright_size(wide) == (200, 30, False)
    assert upright_size(tall) == (30, 200, True)


def test_upright_size_follows_a_slanted_box_not_its_bounding_rectangle():
    # A 200 x 30 box turned ~20 degrees: the bounding rectangle is much bigger.
    slanted = ((0, 70), (188, 6), (198, 34), (10, 98))
    width, height, rotate = upright_size(slanted)
    assert 190 <= width <= 205 and 28 <= height <= 34
    assert rotate is False


def test_order_polygon_puts_corners_clockwise_from_top_left():
    shuffled = [(100, 40), (0, 0), (0, 40), (100, 0)]
    assert order_polygon(shuffled) == ((0, 0), (100, 0), (100, 40), (0, 40))


def test_order_polygon_survives_degenerate_input():
    same = [(5, 5)] * 4
    assert order_polygon(same) == ((5, 5),) * 4


def test_order_polygon_needs_four_points():
    with pytest.raises(ValueError):
        order_polygon([(0, 0), (1, 1), (2, 2)])


def test_reading_order_keeps_a_table_row_together_left_to_right():
    cells = [
        _line("C", 400, 100, 500, 130),
        _line("A", 0, 102, 100, 132),
        _line("B", 200, 98, 300, 128),
        _line("below", 0, 200, 100, 230),
    ]
    assert [ln.text for ln in reading_order(cells)] == ["A", "B", "C", "below"]


def test_reading_order_does_not_merge_two_close_stacked_lines():
    stacked = [_line("second", 0, 40, 200, 70), _line("first", 0, 0, 200, 30)]
    assert [ln.text for ln in reading_order(stacked)] == ["first", "second"]


@pytest.mark.parametrize(
    "text,expected",
    [("Φ520", True), ("QS-FLG-STD2", True), ("QS-FLG-STD", True), ("GB/T", True),
     ("HZ-8800B", True), ("法兰盘 外径 Φ520", True), ("去毛刺", False),
     ("technical requirements", False), ("", False), ("x" * 41 + "1", False),
     # A sentence that happens to contain a number is prose, not a code.
     ("采购部门须在21个工作日内完成供应商资质复核", False)],
)
def test_looks_like_code(text, expected):
    assert looks_like_code(text) is expected


def test_cap_lines_under_the_cap_changes_nothing():
    lines = [_line("a", 0, 0, 10, 10), _line("b", 0, 20, 10, 30)]
    kept, dropped = cap_lines(lines, 5)
    assert kept == lines and dropped == 0


def test_cap_lines_keeps_part_numbers_over_prose_even_when_they_come_last():
    # 10 prose lines first in reading order, then the number somebody will search for.
    words = "甲乙丙丁戊己庚辛壬癸"
    prose = [
        _line(f"设备检修须遵守安全规程{words[i]}", 0, i * 40, 300, i * 40 + 30, conf=0.99)
        for i in range(10)
    ]
    code = _line("QS-FLG-STD", 0, 500, 120, 520, conf=0.6)
    kept, dropped = cap_lines(prose + [code], 3)
    assert dropped == 8
    assert "QS-FLG-STD" in [ln.text for ln in kept]


def test_cap_lines_prefers_larger_text_among_equals_and_returns_reading_order():
    small = _line("note", 0, 0, 100, 10)
    big = _line("TITLE", 0, 100, 300, 160)
    mid = _line("label", 0, 200, 150, 230)
    kept, dropped = cap_lines([small, big, mid], 2)
    assert dropped == 1
    assert [ln.text for ln in kept] == ["TITLE", "label"]


def test_cap_lines_zero_drops_everything():
    kept, dropped = cap_lines([_line("a", 0, 0, 10, 10)], 0)
    assert kept == [] and dropped == 1


def test_engine_config_clamps_the_detector_side_to_the_hard_wall():
    assert EngineConfig(det_side=99999).clamped().det_side == HARD_MAX_DET_SIDE
    assert EngineConfig(det_side=10).clamped().det_side == 320
    assert EngineConfig(det_side=1600).clamped().det_side == 1600
    assert EngineConfig(threads=0, rec_batch=0).clamped().threads == 1
