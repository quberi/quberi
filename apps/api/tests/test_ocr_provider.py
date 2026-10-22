"""LocalOCRProvider and the factory, with a stand-in runner (no process is started)."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from quberi_api.core.config import get_settings
from quberi_api.services.ai import ocr
from quberi_api.services.ai.ocr.base import OcrImageError, OCRProvider, OcrUnavailableError
from quberi_api.services.ai.ocr.engine import decode_image
from quberi_api.services.ai.ocr.local import MAX_PICTURE_BYTES, LocalOCRProvider
from quberi_api.services.ai.ocr.models import ENGINE_NAME, ENGINE_VERSION, EngineConfig


class FakeRunner:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls, self.closed = reply, error, [], False

    async def call(self, picture):
        self.calls.append(picture)
        if self.error:
            raise self.error
        return self.reply

    async def aclose(self):
        self.closed = True


REPLY = {
    "lines": [("QS-FLG-STD", 0.97, [[10, 10], [110, 10], [110, 40], [10, 40]])],
    "width": 640, "height": 480, "dropped": 2, "notes": ["2 line(s) over the cap left out"],
}


async def test_recognize_turns_the_child_reply_into_a_typed_result():
    provider = LocalOCRProvider(runner=FakeRunner(REPLY))
    result = await provider.recognize(b"picture")
    line = result.lines[0]
    assert (line.text, line.confidence) == ("QS-FLG-STD", 0.97)
    assert line.polygon == ((10, 10), (110, 10), (110, 40), (10, 40))
    assert line.bbox == (10, 10, 110, 40)
    assert (result.width, result.height, result.dropped) == (640, 480, 2)
    assert result.engine == ENGINE_NAME and result.version == ENGINE_VERSION
    assert result.text == "QS-FLG-STD"


async def test_the_provider_satisfies_the_protocol():
    assert isinstance(LocalOCRProvider(runner=FakeRunner(REPLY)), OCRProvider)


async def test_empty_and_oversized_pictures_are_refused_before_the_child_is_asked():
    runner = FakeRunner(REPLY)
    provider = LocalOCRProvider(runner=runner)
    with pytest.raises(OcrImageError):
        await provider.recognize(b"")
    with pytest.raises(OcrImageError, match="too large"):
        await provider.recognize(b"x" * (MAX_PICTURE_BYTES + 1))
    assert runner.calls == []


async def test_runner_errors_pass_through_unchanged():
    provider = LocalOCRProvider(runner=FakeRunner(error=OcrUnavailableError("out of memory")))
    with pytest.raises(OcrUnavailableError, match="out of memory"):
        await provider.recognize(b"picture")


async def test_aclose_stops_the_runner():
    runner = FakeRunner(REPLY)
    await LocalOCRProvider(runner=runner).aclose()
    assert runner.closed


def test_the_provider_clamps_an_unsafe_detector_side():
    provider = LocalOCRProvider(EngineConfig(det_side=50_000), runner=FakeRunner(REPLY))
    assert provider.config.det_side == 2000


# -- the picture decoder (runs in the child, but needs no model to test) -----------------
def _png(size=(40, 30), mode="RGB", color="white") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_decode_keeps_the_real_pixels_instead_of_a_thumbnail():
    image = decode_image(_png((3000, 2000)), 40_000_000)
    assert image.size == (3000, 2000) and image.mode == "RGB"


@pytest.mark.parametrize("data", [b"", b"not an image", b"\x89PNG\r\n\x1a\n" + b"junk"])
def test_decode_rejects_unreadable_bytes(data):
    with pytest.raises(OcrImageError):
        decode_image(data, 40_000_000)


def test_decode_enforces_the_pixel_limit():
    with pytest.raises(OcrImageError, match="too many pixels"):
        decode_image(_png((500, 500)), 100_000)


def test_decode_refuses_formats_outside_the_allow_list():
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(buf, format="BMP")
    with pytest.raises(OcrImageError, match="only PNG, JPEG, GIF and WebP"):
        decode_image(buf.getvalue(), 40_000_000)


def test_decode_flattens_other_colour_modes_to_rgb():
    assert decode_image(_png(mode="L", color=128), 40_000_000).mode == "RGB"
    assert decode_image(_png(mode="RGBA", color=(1, 2, 3, 0)), 40_000_000).mode == "RGB"


# -- the factory --------------------------------------------------------------------------
@pytest.fixture
def settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(ocr.factory, "_PROVIDER", None)
    yield s


async def test_factory_builds_one_shared_local_provider(settings, monkeypatch):
    monkeypatch.setattr(settings, "image_ocr_enabled", True)
    monkeypatch.setattr(settings, "ocr_provider", "local")
    first = ocr.get_ocr_provider()
    assert first is ocr.get_ocr_provider()
    assert isinstance(first, LocalOCRProvider)
    await ocr.close_ocr_provider()


def test_factory_reports_ocr_switched_off(settings, monkeypatch):
    monkeypatch.setattr(settings, "image_ocr_enabled", False)
    with pytest.raises(OcrUnavailableError, match="turned off"):
        ocr.get_ocr_provider()


def test_factory_rejects_an_unknown_provider(settings, monkeypatch):
    monkeypatch.setattr(settings, "image_ocr_enabled", True)
    monkeypatch.setattr(settings, "ocr_provider", "carrier-pigeon")
    with pytest.raises(OcrUnavailableError, match="unknown OCR provider"):
        ocr.get_ocr_provider()


# -- the worker stops the engine when it shuts down ---------------------------------------


class _Closable:
    def __init__(self, fail=False):
        self.closed, self.fail = False, fail

    async def close(self):
        self.closed = True

    async def aclose(self):
        if self.fail:
            raise RuntimeError("could not stop")
        self.closed = True


async def test_the_worker_stops_the_ocr_engine_when_it_shuts_down(monkeypatch):
    from quberi_api.db import session as db_session_module
    from quberi_api.workers import rag as worker

    disposed = []

    async def fake_dispose(engine):
        disposed.append(engine)

    monkeypatch.setattr(db_session_module, "dispose_engine", fake_dispose)
    provider = _Closable()
    monkeypatch.setattr(ocr.factory, "_PROVIDER", provider)
    qdrant = _Closable()

    await worker.on_shutdown({"engine": "ENGINE", "qdrant": qdrant})

    assert provider.closed and ocr.factory._PROVIDER is None
    assert qdrant.closed and disposed == ["ENGINE"]


async def test_a_failure_stopping_the_engine_does_not_leave_the_database_open(monkeypatch):
    from quberi_api.db import session as db_session_module
    from quberi_api.workers import rag as worker

    disposed = []

    async def fake_dispose(engine):
        disposed.append(engine)

    monkeypatch.setattr(db_session_module, "dispose_engine", fake_dispose)
    monkeypatch.setattr(ocr.factory, "_PROVIDER", _Closable(fail=True))
    qdrant = _Closable()

    await worker.on_shutdown({"engine": "ENGINE", "qdrant": qdrant})  # must not raise

    assert qdrant.closed and disposed == ["ENGINE"]


async def test_shutting_down_with_no_engine_ever_started_is_quiet(monkeypatch):
    from quberi_api.workers import rag as worker

    monkeypatch.setattr(ocr.factory, "_PROVIDER", None)
    await worker.on_shutdown({})
