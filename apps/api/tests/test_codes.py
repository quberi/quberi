"""Finding part numbers and dimensions, and the forgiving key they are matched by."""

from __future__ import annotations

import pytest

from quberi_api.services.rag.codes import (
    MAX_KEYS_PER_PICTURE,
    MAX_KEYS_PER_TEXT,
    code_key,
    codes_in_lines,
    extract_codes,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("定制法兰 图号 QS-FLG-STD", ["QS-FLG-STD"]),
        ("QS-FLG-STD2", ["QS-FLG-STD2"]),
        ("法兰外径 Φ520 内孔 Φ260", ["Φ520", "Φ260"]),
        ("Φ20H7", ["Φ20H7"]),
        ("设备铭牌 型号 HZ-8800B 出厂编号 20261019-0042", ["HZ-8800B", "20261019-0042"]),
        ("图号 DWG-2026-0197 版本 B", ["DWG-2026-0197"]),
        ("深沟球轴承 6205-2RS 内径 25", ["6205-2RS"]),
        ("材料 Q235B 热处理 调质", ["Q235B"]),
        ("GB/T 276-2013", ["GB/T276-2013"]),
        ("DIN 912 M12", ["DIN912", "M12"]),
        ("技术要求 1. 去毛刺 2. 锐边倒钝 R0.5", ["R0.5"]),
    ],
)
def test_codes_are_found_in_drawing_text(text, expected):
    assert extract_codes(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "去毛刺",
        "第3条 设备检修",
        "I have 2 apples",
        "ABC",
        "state-of-the-art well-known design",
        "",
    ],
)
def test_ordinary_text_has_no_codes(text):
    assert extract_codes(text) == []


def test_a_label_in_front_of_a_dimension_is_not_part_of_it():
    assert extract_codes("法兰外径Φ520") == ["Φ520"]


def test_a_dimension_keeps_its_main_number_not_the_tolerance():
    # "120" alone is too common to be a useful key, but the main value is kept.
    assert extract_codes("孔距 120±0.05") == ["120"]


def test_a_repeated_code_is_listed_once_in_order():
    assert extract_codes("M12 Φ20 M12 Φ20 R0.5") == ["M12", "Φ20", "R0.5"]


def test_a_text_cannot_produce_an_unbounded_number_of_keys():
    text = " ".join(f"AB{i:04d}" for i in range(500))
    assert len(extract_codes(text)) == MAX_KEYS_PER_TEXT
    assert len(codes_in_lines([text] * 20)) <= MAX_KEYS_PER_PICTURE


def test_codes_in_lines_merges_lines_without_repeats():
    assert codes_in_lines(["Φ520 M12", "M12 Q235B", "去毛刺"]) == ["Φ520", "M12", "Q235B"]


# -- the forgiving key --------------------------------------------------------------


@pytest.mark.parametrize(
    "variant", ["Φ520", "ϕ520", "φ520", "ф520", "Ø520", "ø520", "∅520", "⌀520", "中520"]
)
def test_every_way_of_writing_or_misreading_a_diameter_gives_one_key(variant):
    assert code_key(variant) == code_key("Φ520") == "Φ520"


def test_case_and_width_do_not_matter():
    assert code_key("q235b") == code_key("Q235B") == code_key("Ｑ２３５Ｂ")


def test_a_diameter_sign_misread_as_a_chinese_character_inside_a_sentence_is_found():
    assert extract_codes("法兰外径中520内孔Ø260") == ["Φ520", "Φ260"]


def test_the_key_never_turns_a_digit_into_a_letter():
    # The number somebody searches for must not be answered with a different number.
    assert code_key("Q235B") != code_key("Q2358")
    assert code_key("Φ520") != code_key("Φ522")
    assert code_key("M12") != code_key("MI2")


def test_the_diameter_character_is_only_folded_where_it_stands_for_a_diameter():
    # 中 in ordinary words is not a code at all.
    assert extract_codes("中国制造 中间件") == []


def test_a_query_and_a_picture_agree_when_they_write_the_same_code_differently():
    in_picture = set(extract_codes("法兰外径中520"))      # misread when the picture was read
    in_query = set(extract_codes("Ø520 的法兰"))          # typed by the person searching
    assert in_picture & in_query == {"Φ520"}
