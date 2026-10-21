"""The real PP-OCRv5 models, end to end through the child process.

Skipped unless ``QUBERI_TEST_REAL_OCR_MODEL`` points at a Hugging Face cache that already
holds the pinned weights (about 20 MB), and torch, torchvision and OpenCV import. Nothing is
downloaded by the test run itself.
"""

from __future__ import annotations

import importlib.util
import io
import os

import pytest
from PIL import Image, ImageDraw, ImageFont

from quberi_api.services.ai.ocr.local import LocalOCRProvider
from quberi_api.services.ai.ocr.models import EngineConfig

_CACHE = os.getenv("QUBERI_TEST_REAL_OCR_MODEL")
_FONT = os.getenv("QUBERI_TEST_OCR_FONT", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf")

pytestmark = pytest.mark.skipif(
    not _CACHE
    or not all(importlib.util.find_spec(m) for m in ("torch", "torchvision", "cv2"))
    or not os.path.exists(_FONT),
    reason="set QUBERI_TEST_REAL_OCR_MODEL (and have torch, torchvision, cv2 and a CJK font)",
)


def _page(lines, size=(1400, 700)) -> bytes:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(_FONT, 44)
    for i, text in enumerate(lines):
        draw.text((60, 50 + i * 100), text, fill="black", font=font)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
async def provider():
    p = LocalOCRProvider(
        EngineConfig(hf_home=_CACHE, threads=2),
        max_memory_mb=2500,
        request_timeout_s=120,
        startup_timeout_s=300,
        idle_unload_s=0,
    )
    yield p
    await p.aclose()


async def test_reads_chinese_codes_and_dimensions(provider):
    wanted = ["定制法兰 图号 QS-FLG-STD", "法兰外径 Φ520 内孔 Φ260", "底盘支架 孔距 120±0.05"]
    result = await provider.recognize(_page(wanted))
    joined = "".join(result.text.split())
    for text in wanted:
        assert "".join(text.split()) in joined, f"missing: {text!r} in {result.text!r}"
    assert result.width == 1400 and result.height == 700
    assert [ln.bbox[1] for ln in result.lines] == sorted(ln.bbox[1] for ln in result.lines)
    assert all(0.5 <= ln.confidence <= 1.0 for ln in result.lines)


async def test_reads_text_turned_a_quarter_turn_either_way(provider):
    page = Image.open(io.BytesIO(_page(["M12 Φ520H7"], size=(900, 200)))).convert("RGB")
    for angle in (90, 270):
        buf = io.BytesIO()
        page.rotate(angle, expand=True).save(buf, format="PNG")
        result = await provider.recognize(buf.getvalue())
        assert "Φ520H7" in "".join(result.text.split()), f"angle {angle}: {result.text!r}"


async def test_a_blank_page_gives_no_lines_not_an_error(provider):
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), "white").save(buf, format="PNG")
    assert (await provider.recognize(buf.getvalue())).lines == []
