"""The image embedder: safe decoding, an integrity-checked model load, lazy loading.

The real BGE-VL weights are not loaded here (that is a ~300 MB download); a
stand-in model with the same ``encode`` signature is injected. The real model
is exercised once by ``test_real_model_*`` when ``QUBERI_TEST_REAL_IMAGE_MODEL``
points at a Hugging Face cache that has it.
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import types

import numpy as np
import pytest
from PIL import Image

from quberi_api.services.ai import local_image_embedding as lie
from quberi_api.services.ai.local_image_embedding import (
    BGE_VL_DIM,
    ImageDecodeError,
    ImageModelIntegrityError,
    LocalImageEmbedder,
    prepare_image,
)


def _png(size=(32, 24), color="red", mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format="PNG")
    return buf.getvalue()


def _fmt(fmt: str, size=(16, 16)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "blue").save(buf, format=fmt)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Decoding untrusted bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF", "WEBP"])
def test_the_four_accepted_formats_decode_to_rgb(fmt):
    image = prepare_image(_fmt(fmt))
    assert image.mode == "RGB"
    assert image.size == (16, 16)


def test_a_transparent_or_palette_picture_becomes_plain_rgb():
    assert prepare_image(_png(mode="RGBA", color=(255, 0, 0, 0))).mode == "RGB"
    buf = io.BytesIO()
    Image.new("P", (8, 8)).save(buf, format="PNG")
    assert prepare_image(buf.getvalue()).mode == "RGB"


def test_a_large_picture_is_shrunk_before_it_reaches_the_model():
    image = prepare_image(_png(size=(2000, 1000)))
    assert max(image.size) <= 512
    assert image.size[0] == 2 * image.size[1] or abs(image.size[0] - 2 * image.size[1]) <= 1


def test_an_animated_gif_uses_its_first_frame():
    frames = [Image.new("RGB", (10, 10), c) for c in ("red", "green", "blue")]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=50)
    image = prepare_image(buf.getvalue())
    assert image.size == (10, 10)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not an image at all",
        b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
        b"%PDF-1.7\n" + b"x" * 100,
        _png()[:40],  # truncated mid-header
        _png(size=(200, 200), color="white")[:70],  # truncated before the pixel data ends
        _fmt("JPEG", size=(200, 200))[:300],  # a JPEG cut off mid-image
    ],
)
def test_bytes_that_are_not_a_readable_picture_are_refused(data):
    with pytest.raises(ImageDecodeError):
        prepare_image(data)


@pytest.mark.parametrize("fmt", ["BMP", "TIFF", "ICO"])
def test_other_picture_formats_are_not_accepted(fmt):
    with pytest.raises(ImageDecodeError, match="only PNG, JPEG, GIF and WebP"):
        prepare_image(_fmt(fmt))


def test_a_picture_with_too_many_pixels_is_refused_without_decoding_it():
    # A small file that claims a huge canvas: the classic decompression bomb.
    buf = io.BytesIO()
    Image.new("1", (9000, 9000), 0).save(buf, format="PNG")
    assert len(buf.getvalue()) < 50_000
    with pytest.raises(ImageDecodeError, match="too many pixels"):
        prepare_image(buf.getvalue())


def test_the_pixel_limit_sits_exactly_at_the_boundary(monkeypatch):
    monkeypatch.setattr(lie, "MAX_IMAGE_PIXELS", 100)
    assert prepare_image(_png(size=(10, 10))).size == (10, 10)
    with pytest.raises(ImageDecodeError, match="too many pixels"):
        prepare_image(_png(size=(11, 10)))


# ---------------------------------------------------------------------------
# A stand-in model
# ---------------------------------------------------------------------------


class FakeModel:
    def __init__(self, dim=BGE_VL_DIM):
        self.dim = dim
        self.calls: list[int] = []

    def encode(self, items, normalize_embeddings=True, convert_to_numpy=True):
        self.calls.append(len(items))
        out = []
        for item in items:
            seed = hash(str(getattr(item, "size", item))) % 1000
            v = np.zeros(self.dim, dtype=np.float32)
            v[seed % self.dim] = 1.0
            out.append(v)
        return np.array(out)


async def test_embeds_pictures_and_sentences_into_the_same_size():
    model = FakeModel()
    embedder = LocalImageEmbedder(model=model)
    pics = await embedder.embed_images([_png(), _png(color="blue")])
    words = await embedder.embed_texts(["a red part", "  a blue part  "])
    assert len(pics) == len(words) == 2
    assert all(len(v) == BGE_VL_DIM for v in pics + words)
    assert embedder.vector_dim == BGE_VL_DIM


async def test_empty_input_is_empty_output_and_loads_nothing():
    embedder = LocalImageEmbedder()
    assert await embedder.embed_images([]) == []
    assert await embedder.embed_texts([]) == []
    assert embedder._model is None


async def test_work_is_done_in_small_batches():
    model = FakeModel()
    embedder = LocalImageEmbedder(model=model)
    await embedder.embed_images([_png(size=(8 + i, 8)) for i in range(20)])
    assert model.calls == [8, 8, 4]


async def test_a_bad_picture_stops_the_whole_batch_before_the_model_runs():
    model = FakeModel()
    embedder = LocalImageEmbedder(model=model)
    with pytest.raises(ImageDecodeError):
        await embedder.embed_images([_png(), b"junk", _png()])
    assert model.calls == []


async def test_a_model_of_the_wrong_size_is_caught():
    embedder = LocalImageEmbedder(model=FakeModel(dim=300))
    with pytest.raises(RuntimeError, match="300-d vectors, expected 512"):
        await embedder.embed_texts(["x"])


def test_the_embedder_is_shared_and_starts_empty():
    lie._EMBEDDERS.clear()
    first = lie.get_image_embedder()
    assert lie.get_image_embedder() is first
    assert first._model is None  # nothing loaded until a picture or sentence arrives


# ---------------------------------------------------------------------------
# The model's own code is checked before it runs
# ---------------------------------------------------------------------------


def _fake_hub(monkeypatch, tmp_path, contents: dict[str, bytes]):
    """Make hf_hub_download return local files with the given contents."""

    paths = {}
    for name, data in contents.items():
        p = tmp_path / name
        p.write_bytes(data)
        paths[name] = str(p)
    fake = types.SimpleNamespace(
        hf_hub_download=lambda repo, filename, revision=None, cache_dir=None: paths[filename]
    )
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)


def test_unchanged_remote_code_passes(monkeypatch, tmp_path):
    good = {name: name.encode() for name in lie.BGE_VL_REMOTE_CODE_SHA256}
    monkeypatch.setattr(
        lie, "BGE_VL_REMOTE_CODE_SHA256",
        {n: hashlib.sha256(d).hexdigest() for n, d in good.items()},
    )
    _fake_hub(monkeypatch, tmp_path, good)
    lie._verify_remote_code(str(tmp_path))  # no error


@pytest.mark.parametrize("victim", list(lie.BGE_VL_REMOTE_CODE_SHA256))
def test_changed_remote_code_is_refused(monkeypatch, tmp_path, victim):
    good = {name: name.encode() for name in lie.BGE_VL_REMOTE_CODE_SHA256}
    monkeypatch.setattr(
        lie, "BGE_VL_REMOTE_CODE_SHA256",
        {n: hashlib.sha256(d).hexdigest() for n, d in good.items()},
    )
    tampered = dict(good)
    tampered[victim] = b"import os; os.system('echo hi')"
    _fake_hub(monkeypatch, tmp_path, tampered)
    with pytest.raises(ImageModelIntegrityError, match=victim):
        lie._verify_remote_code(str(tmp_path))


async def test_the_model_is_never_constructed_when_the_check_fails(monkeypatch):
    built = []

    class Boom:
        def __init__(self, *a, **k):
            built.append((a, k))

    fake_st = types.SimpleNamespace(SentenceTransformer=Boom)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)

    def refuse(_home):
        raise ImageModelIntegrityError("tampered")

    monkeypatch.setattr(lie, "_verify_remote_code", refuse)
    with pytest.raises(ImageModelIntegrityError):
        await LocalImageEmbedder()._ensure_model()
    assert built == []


async def test_the_model_loads_from_the_pinned_revision_once_the_check_passes(monkeypatch):
    seen = {}

    class Fake:
        def __init__(self, repo, **kwargs):
            seen["repo"], seen["kwargs"] = repo, kwargs

    fake_st = types.SimpleNamespace(SentenceTransformer=Fake)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)
    monkeypatch.setattr(lie, "_verify_remote_code", lambda _home: None)
    embedder = LocalImageEmbedder()
    first = await embedder._ensure_model()
    assert await embedder._ensure_model() is first  # loaded once
    assert seen["repo"] == "BAAI/BGE-VL-base"
    assert seen["kwargs"]["revision"] == "cc4c733ed997dbee4ac70ccffb911e70c9c24b93"
    assert seen["kwargs"]["trust_remote_code"] is True
    assert seen["kwargs"]["device"] == "cpu"


async def test_an_unknown_model_id_is_refused():
    with pytest.raises(KeyError):
        await LocalImageEmbedder("some-other-model")._ensure_model()


def test_the_audited_hashes_are_complete_sha256_values():
    for name, digest in lie.BGE_VL_REMOTE_CODE_SHA256.items():
        assert name.endswith(".py")
        assert len(digest) == 64 and int(digest, 16) >= 0
    assert len(lie.BGE_VL_REVISION) == 40


# ---------------------------------------------------------------------------
# The real model (optional)
# ---------------------------------------------------------------------------

REAL = os.environ.get("QUBERI_TEST_REAL_IMAGE_MODEL")


@pytest.mark.skipif(not REAL, reason="set QUBERI_TEST_REAL_IMAGE_MODEL=<HF cache dir> to run")
async def test_real_model_puts_a_picture_next_to_its_description(monkeypatch):
    monkeypatch.setenv("HF_HOME", REAL)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    embedder = LocalImageEmbedder()
    pics = await embedder.embed_images([_png(color=c) for c in ("red", "blue", "green")])
    words = await embedder.embed_texts([f"a solid {c} image" for c in ("red", "blue", "green")])
    sim = np.array(words) @ np.array(pics).T
    assert [int(sim[i].argmax()) for i in range(3)] == [0, 1, 2]
    assert all(abs(np.linalg.norm(v) - 1.0) < 0.01 for v in pics)
