"""Chunks know which pages and blocks their text came from.

Two things are pinned. First, the packing itself is unchanged: ``chunk_text`` gives the same
windows it always did (checked against the original implementation on many generated
inputs). Second, the recorded positions are true: a citation that names fewer places than
the text really came from would send a reader to the wrong page.
"""

from __future__ import annotations

import random
import re

import pytest

from quberi_api.services.rag.chunk import Chunk, chunk_text, chunk_units

FILLER = ["alpha", "beta", "gamma", "delta", "supplier", "payment", "goods", "invoice", "within"]


def block(marker: str, words: int, page: int, index: int, label: str = "text_block"):
    body = " ".join(["w"] * words)
    return (f"{marker} {body} {marker}", (page, index, label))


# -- positions on a few readable cases ---------------------------------------------------


def test_a_chunk_records_the_first_and_last_page_and_block_it_touches():
    units = [block("AAA", 5, 1, 0), block("BBB", 5, 1, 1), block("CCC", 5, 2, 0)]
    (chunk,) = chunk_units(units)
    assert (chunk.page_start, chunk.page_end) == (1, 2)
    assert (chunk.unit_start, chunk.unit_end) == (0, 0)  # first block of page 1, last of page 2
    assert chunk.unit_label == "text_block"


def test_each_chunk_gets_its_own_range():
    units = [block(m, 30, 1, i) for i, m in enumerate(["AAA", "BBB", "CCC", "DDD"])]
    chunks = chunk_units(units, max_chars=140, overlap_chars=0)
    assert [(c.unit_start, c.unit_end) for c in chunks] == [(0, 1), (2, 3)]


def test_the_repeated_tail_counts_as_part_of_the_next_chunk():
    units = [block(m, 30, 1, i) for i, m in enumerate(["AAA", "BBB", "CCC", "DDD"])]
    chunks = chunk_units(units, max_chars=140, overlap_chars=60)
    # Each window starts with the end of the previous one, so it reaches back a block.
    assert [(c.unit_start, c.unit_end) for c in chunks] == [(0, 1), (1, 2), (2, 3)]
    assert "BBB" in chunks[1].text and chunks[1].unit_start == 1


def test_a_tail_that_holds_several_whole_blocks_names_all_of_them():
    # The last chunk is only the tail of the one before it, which contained two blocks.
    units = [block("PPP", 30, 1, 7), block("QQQ", 30, 1, 8), block("RRR", 3, 1, 9)]
    last = chunk_units(units, max_chars=120, overlap_chars=60)[-1]
    assert "QQQ" in last.text and "RRR" in last.text
    assert (last.unit_start, last.unit_end) == (8, 9)


def test_a_block_longer_than_the_window_keeps_its_position_in_every_piece():
    units = [block("AAA", 600, 4, 2)]
    chunks = chunk_units(units, max_chars=300, overlap_chars=0)
    assert len(chunks) > 3
    assert {(c.page_start, c.page_end, c.unit_start, c.unit_end) for c in chunks} == {(4, 4, 2, 2)}


def test_text_recognised_from_a_picture_marks_the_whole_chunk_as_recognised():
    units = [block("AAA", 5, 3, 0, "text_block"), block("BBB", 5, 3, 1, "ocr_block")]
    (chunk,) = chunk_units(units)
    assert chunk.unit_label == "ocr_block"  # never looks more certain than its weakest part


def test_blocks_without_text_are_skipped_and_nothing_at_all_is_no_chunks():
    assert chunk_units([]) == []
    assert chunk_units([("", (1, 0, "text_block")), ("   ", (1, 1, "text_block"))]) == []
    (chunk,) = chunk_units([("", (1, 0, "text_block")), block("AAA", 5, 1, 1)])
    assert chunk.unit_start == 1


def test_plain_text_has_no_positions_at_all():
    (chunk,) = chunk_text("just a few words of plain text, long enough to be kept as a chunk")
    assert (chunk.page_start, chunk.page_end, chunk.unit_start, chunk.unit_end) == (
        None, None, None, None,
    )
    assert chunk.unit_label == ""


def test_the_size_rules_apply_to_positioned_text_too():
    with pytest.raises(ValueError):
        chunk_units([block("A", 3, 1, 0)], max_chars=0)
    with pytest.raises(ValueError):
        chunk_units([block("A", 3, 1, 0)], max_chars=100, overlap_chars=100)


# -- the property that matters: positions are true --------------------------------------


def _trial(seed: int):
    rnd = random.Random(seed)
    units, marker_of, page, index = [], {}, 1, 0
    for n in range(rnd.randint(1, 60)):
        if rnd.random() < 0.18:
            page, index = page + 1, 0
        marker = f"ZQX{seed:03d}{n:03d}"
        words = rnd.choice([3, 8, 20, 60, 200, 400])
        body = " ".join(rnd.choice(FILLER) for _ in range(words))
        text = f"{marker} {body} {marker}"
        units.append((text, (page, index, "text_block")))
        marker_of[marker] = (page, index)
        index += 1
    kw = rnd.choice(
        [{}, {"max_chars": 300, "overlap_chars": 40}, {"max_chars": 120, "overlap_chars": 60},
         {"max_chars": 800, "overlap_chars": 0}]
    )
    return units, marker_of, kw


def test_a_chunk_never_names_fewer_places_than_its_text_came_from():
    checked = exact = 0
    for seed in range(120):
        units, marker_of, kw = _trial(seed)
        for chunk in chunk_units(units, **kw):
            present = {marker_of[m] for m in re.findall(r"ZQX\d{6}", chunk.text) if m in marker_of}
            if not present:
                continue
            checked += 1
            first, last = min(present), max(present)
            got_first = (chunk.page_start, chunk.unit_start)
            got_last = (chunk.page_end, chunk.unit_end)
            assert got_first <= first and got_last >= last, (
                seed, kw, first, last, got_first, got_last,
            )
            exact += (got_first, got_last) == (first, last)
    assert checked > 3000
    assert exact / checked > 0.95  # wider only when a long block's far end is in the tail


def test_positions_are_ordered_and_inside_the_document():
    for seed in range(60):
        units, marker_of, kw = _trial(seed)
        last_page = max(p for _, (p, _, _) in units)
        for chunk in chunk_units(units, **kw):
            assert 1 <= chunk.page_start <= chunk.page_end <= last_page
            if chunk.page_start == chunk.page_end:
                assert chunk.unit_start <= chunk.unit_end


# -- packing is unchanged ----------------------------------------------------------------


def test_packing_with_positions_is_the_same_packing_as_without():
    """Same windows, same text, same offsets: positions are an addition, not a change."""
    for seed in range(60):
        units, _, kw = _trial(seed)
        joined = "\n\n".join(t for t, _ in units)
        plain = chunk_text(joined, **kw)
        positioned = chunk_units(units, **kw)
        assert [(c.text, c.char_start, c.char_end) for c in positioned] == [
            (c.text, c.char_start, c.char_end) for c in plain
        ]


def test_text_without_positions_is_packed_exactly_as_the_original_chunker_did():
    """The rewrite changed nothing: compared with the original code, window for window."""
    from tests.chunk_reference import chunk_text as original

    rnd = random.Random(20261021)
    cjk = list("设备检修须遵守安全规程采购部门完成供应商资质复核图纸变更同步更新版本通知相关")
    compared = 0
    for _ in range(300):
        paragraphs = []
        for _ in range(rnd.choice([0, 1, 2, 3, 5, 10, 30])):
            if rnd.random() < 0.5:
                sentences = [
                    "".join(rnd.choice(cjk) for _ in range(rnd.randint(6, 40)))
                    + rnd.choice(["。", "！", ""])
                    for _ in range(rnd.randint(1, 12))
                ]
            else:
                sentences = [
                    " ".join(rnd.choice(FILLER) for _ in range(rnd.randint(4, 25)))
                    + rnd.choice([".", ""])
                    for _ in range(rnd.randint(1, 12))
                ]
            paragraphs.append(rnd.choice(["", " ", "\n"]).join(sentences))
        text = rnd.choice(["\n\n", "\n\n\n", "\n \n"]).join(paragraphs)
        if rnd.random() < 0.05:
            text = "x" * rnd.randint(1, 5000)
        kw = rnd.choice(
            [{}, {"max_chars": 600, "overlap_chars": 50}, {"max_chars": 200, "overlap_chars": 0},
             {"max_chars": 90, "overlap_chars": 30}]
        )
        want = [(c.index, c.text, c.char_start, c.char_end) for c in original(text, **kw)]
        got = [(c.index, c.text, c.char_start, c.char_end) for c in chunk_text(text, **kw)]
        assert got == want
        compared += len(want)
    assert compared > 1500


def test_chunks_are_still_plain_values_with_the_old_four_fields_first():
    chunk = Chunk(index=0, text="t", char_start=0, char_end=1)
    assert chunk.page_start is None and chunk.unit_label == ""
    assert Chunk(0, "t", 0, 1) == chunk  # positional construction as before
