"""In-process image embedding (BGE-VL), for finding pictures by picture or by words.

BGE-VL puts images and sentences in one 512-dimensional space, so the same
vectors answer "pictures that look like this one" and "pictures matching this
description". It runs inside the worker/API process through
``sentence-transformers``, like the text models in :mod:`local_embedding`.

Two safety properties matter here:

* **The model's own code is pinned and checked.** BGE-VL ships two Python files
  that ``trust_remote_code`` executes. They were read before this was written
  (they import only torch, transformers, Pillow and sentence-transformers, and
  make no process or network calls). The model is loaded from a fixed git
  revision, and before it is imported the files' SHA-256 are compared with the
  audited ones; a mismatch (a changed file, a tampered mirror) aborts the load.
* **A picture is untrusted input.** Decoding is done with Pillow under a pixel
  limit (decompression bombs), always to plain RGB, and shrunk before it reaches
  the model. Nothing here writes the picture anywhere.

The model loads on first use, not at start-up, so an instance nobody sends
pictures to pays nothing for it.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
from collections.abc import Sequence
from typing import Any

_log = logging.getLogger(__name__)

BGE_VL_REPO = "BAAI/BGE-VL-base"
# The audited revision. Changing it means re-reading the remote code and
# updating the hashes below.
BGE_VL_REVISION = "cc4c733ed997dbee4ac70ccffb911e70c9c24b93"
BGE_VL_REMOTE_CODE_SHA256 = {
    "bge_vl_clip_transformer.py": (
        "19869b0ae179811bb040a560cc1f8c2f9fbd6c8e11e84aeb37835fcda6616449"
    ),
    "modeling_MMRet_CLIP.py": (
        "19a630f89e381dcc0febad3c3d1c392fac7a241819432f08dee9a56a5b2d0689"
    ),
}
BGE_VL_DIM = 512

_MODEL_TO_HF = {"bge-vl-base": BGE_VL_REPO}

# Pillow refuses images with more pixels than this (a small file can expand to
# gigabytes of pixels). 40 megapixels is a large photo, far above anything a
# part photo or screenshot needs.
MAX_IMAGE_PIXELS = 40_000_000
# The longest side is shrunk to this before embedding; CLIP-style models look at
# a 224-pixel view anyway, so more only costs time and memory.
_MAX_SIDE = 512
_BATCH = 8


class ImageDecodeError(ValueError):
    """The bytes are not an image we can safely turn into a vector."""


class ImageModelIntegrityError(RuntimeError):
    """The model's remote code is not the audited code."""


def prepare_image(data: bytes) -> Any:
    """Decode untrusted bytes into a small RGB Pillow image, or raise.

    Only still frames of PNG, JPEG, GIF and WebP are accepted (by what Pillow
    detects, not by any label). An animated file uses its first frame.
    """

    from PIL import Image, UnidentifiedImageError

    if not data:
        raise ImageDecodeError("the image is empty")
    # Decompression-bomb guard: Pillow raises when a file claims more pixels
    # than this (and warns at half of it, which we also treat as an error).
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        image = Image.open(io.BytesIO(data))
        if image.format not in ("PNG", "JPEG", "GIF", "WEBP"):
            raise ImageDecodeError("only PNG, JPEG, GIF and WebP pictures are accepted")
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise ImageDecodeError("the picture has too many pixels")
        image.load()
    except ImageDecodeError:
        raise
    except Image.DecompressionBombError as exc:
        raise ImageDecodeError("the picture has too many pixels") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageDecodeError("the file is not a readable picture") from exc
    rgb = image.convert("RGB")
    rgb.thumbnail((_MAX_SIDE, _MAX_SIDE))
    return rgb


def _verify_remote_code(hf_home: str | None) -> None:
    """Compare the downloaded remote-code files with the audited hashes."""

    from huggingface_hub import hf_hub_download

    for name, expected in BGE_VL_REMOTE_CODE_SHA256.items():
        path = hf_hub_download(
            BGE_VL_REPO, name, revision=BGE_VL_REVISION, cache_dir=hf_home
        )
        with open(path, "rb") as handle:
            actual = hashlib.sha256(handle.read()).hexdigest()
        if actual != expected:
            raise ImageModelIntegrityError(
                f"{name} is not the audited file for revision {BGE_VL_REVISION[:8]}; "
                "refusing to run it"
            )


class LocalImageEmbedder:
    """Holds one loaded BGE-VL model; embeds pictures and sentences.

    ``model`` may be injected (tests pass a stand-in); otherwise it is loaded
    from the pinned revision the first time it is needed.
    """

    def __init__(self, model_id: str = "bge-vl-base", model: Any | None = None) -> None:
        self.model_id = model_id
        self._model = model
        self._load_lock = asyncio.Lock()
        self._inference_lock = asyncio.Lock()

    @property
    def vector_dim(self) -> int:
        return BGE_VL_DIM

    async def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is not None:
                return self._model
            repo = _MODEL_TO_HF.get(self.model_id)
            if repo is None:
                raise KeyError(f"no HF repo mapping for image model {self.model_id!r}")
            _log.info(
                "loading image embedding model %s (%s@%s)",
                self.model_id, repo, BGE_VL_REVISION[:8],
            )
            hf_home = os.getenv("HF_HOME")

            def _load() -> Any:
                _verify_remote_code(hf_home)
                from sentence_transformers import SentenceTransformer

                return SentenceTransformer(
                    repo,
                    revision=BGE_VL_REVISION,
                    cache_folder=hf_home,
                    trust_remote_code=True,
                    device="cpu",
                )

            loop = asyncio.get_running_loop()
            self._model = await loop.run_in_executor(None, _load)
            return self._model

    async def _encode(self, items: Sequence[Any]) -> list[list[float]]:
        model = await self._ensure_model()
        out: list[list[float]] = []
        async with self._inference_lock:
            loop = asyncio.get_running_loop()
            for start in range(0, len(items), _BATCH):
                batch = list(items[start : start + _BATCH])
                vectors = await loop.run_in_executor(
                    None,
                    lambda b=batch: model.encode(
                        b, normalize_embeddings=True, convert_to_numpy=True
                    ),
                )
                for vector in vectors:
                    row = [float(x) for x in vector.tolist()]
                    if len(row) != BGE_VL_DIM:
                        raise RuntimeError(
                            f"image model {self.model_id} produced {len(row)}-d vectors, "
                            f"expected {BGE_VL_DIM}"
                        )
                    out.append(row)
        return out

    async def embed_images(self, images: Sequence[bytes]) -> list[list[float]]:
        """One vector per picture. Raises :class:`ImageDecodeError` for a bad file."""

        if not images:
            return []
        loop = asyncio.get_running_loop()
        prepared = [await loop.run_in_executor(None, prepare_image, data) for data in images]
        return await self._encode(prepared)

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per sentence, in the same space as the pictures."""

        if not texts:
            return []
        return await self._encode([t.strip() for t in texts])


_EMBEDDERS: dict[str, LocalImageEmbedder] = {}


def get_image_embedder(model_id: str = "bge-vl-base") -> LocalImageEmbedder:
    """The process-wide embedder for a model (created empty; loads on first use)."""

    embedder = _EMBEDDERS.get(model_id)
    if embedder is None:
        embedder = _EMBEDDERS[model_id] = LocalImageEmbedder(model_id)
    return embedder
