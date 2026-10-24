"""In-process image embedding, for finding pictures by picture or by words.

Two models are known. Both put pictures and sentences in one space, so the same
vectors answer "pictures that look like this one" and "pictures matching this
description":

* ``siglip2-base`` (the default): Google SigLIP2, Apache-2.0, understands Chinese,
  English and ~100 other languages. Plain ``transformers`` and safetensors; no
  code from the model repository is ever executed.
* ``bge-vl-base``: BAAI BGE-VL, MIT, **English only** (its text side is OpenAI
  CLIP). Kept so an instance can be pointed back at it and so its old vectors can
  be cleaned up; it needs ``trust_remote_code``, which is pinned and hash-checked.

Safety properties that matter here:

* **Pinned revisions.** Each model loads from a fixed git revision, never "main".
* **A picture is untrusted input.** Decoding is done with Pillow under a pixel
  limit (decompression bombs), always to plain RGB, and shrunk before it reaches
  the model. Nothing here writes the picture anywhere.

Models load on first use, not at start-up, so an instance nobody sends pictures to
pays nothing for them. SigLIP2 loads its two halves separately: the indexing worker
only ever needs the picture half, and the API only loads the text half when someone
searches with words.
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

SIGLIP2_REPO = "google/siglip2-base-patch16-224"
# Pinned: the revision that was evaluated. Changing it means re-running the checks.
SIGLIP2_REVISION = "75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2"
SIGLIP2_DIM = 768
# The model reads at most 64 tokens of a description.
SIGLIP2_MAX_TOKENS = 64

DEFAULT_IMAGE_MODEL = "siglip2-base"
# Every picture model this build knows, with its vector size. A picture's vector may
# still sit under an older one until it is re-indexed, so anything that *removes*
# vectors walks this whole list.
IMAGE_MODEL_DIMS: dict[str, int] = {"siglip2-base": SIGLIP2_DIM, "bge-vl-base": BGE_VL_DIM}


def image_vector_dim(model_id: str) -> int:
    """Vector size of a picture model; ``KeyError`` for one this build does not know."""

    try:
        return IMAGE_MODEL_DIMS[model_id]
    except KeyError:
        raise KeyError(f"unknown image model {model_id!r}") from None


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


def prepare_image(data: bytes, *, max_side: int = _MAX_SIDE) -> Any:
    """Decode untrusted bytes into a small RGB Pillow image, or raise.

    Only still frames of PNG, JPEG, GIF and WebP are accepted (by what Pillow
    detects, not by any label). An animated file uses its first frame. The picture model
    needs no more than ``_MAX_SIDE`` px; a caller that wants to show the picture larger
    (a page image) passes a bigger ``max_side``.
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
    rgb.thumbnail((max_side, max_side))
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


class SiglipImageEmbedder:
    """SigLIP2 base: pictures and sentences (any language) into one 768-d space.

    The picture half and the text half are separate ``transformers`` models, each
    loaded the first time it is needed. The text half's word table (786 MB as
    32-bit floats, a plain lookup) is held as 16-bit floats and widened on lookup,
    which changes the result by less than 1 part in 100,000 and saves ~390 MB.

    ``vision``/``text``/``processor`` may be injected (tests pass stand-ins).
    """

    def __init__(
        self,
        model_id: str = "siglip2-base",
        *,
        vision: Any | None = None,
        text: Any | None = None,
        processor: Any | None = None,
    ) -> None:
        self.model_id = model_id
        self._vision = vision
        self._text = text
        self._processor = processor
        self._load_lock = asyncio.Lock()
        self._inference_lock = asyncio.Lock()

    @property
    def vector_dim(self) -> int:
        return SIGLIP2_DIM

    # -- loading ----------------------------------------------------------------
    @staticmethod
    def _cache_dir() -> str | None:
        return os.getenv("HF_HOME")

    def _load_processor(self) -> Any:
        from transformers import AutoProcessor

        return AutoProcessor.from_pretrained(
            SIGLIP2_REPO,
            revision=SIGLIP2_REVISION,
            cache_dir=self._cache_dir(),
            trust_remote_code=False,
        )

    def _load_vision(self) -> Any:
        from transformers import SiglipVisionModel

        return SiglipVisionModel.from_pretrained(
            SIGLIP2_REPO,
            revision=SIGLIP2_REVISION,
            cache_dir=self._cache_dir(),
            trust_remote_code=False,
            use_safetensors=True,
        ).eval()

    def _load_text(self) -> Any:
        import torch
        from transformers import SiglipTextModel

        # Load everything at 16 bits first, so the 32-bit word table never exists in
        # memory, then widen all but that table.
        model = SiglipTextModel.from_pretrained(
            SIGLIP2_REPO,
            revision=SIGLIP2_REVISION,
            cache_dir=self._cache_dir(),
            trust_remote_code=False,
            use_safetensors=True,
            dtype=torch.bfloat16,
        ).eval()
        table = model.embeddings.token_embedding
        for param in model.parameters():
            if param is not table.weight:
                param.data = param.data.to(torch.float32)
        table.register_forward_hook(lambda _module, _inputs, out: out.float())
        return model

    async def _ensure(self, attr: str, loader: Any) -> Any:
        current = getattr(self, attr)
        if current is not None:
            return current
        async with self._load_lock:
            current = getattr(self, attr)
            if current is not None:
                return current
            _log.info("loading image embedding model %s (%s: %s@%s)", self.model_id, attr[1:], SIGLIP2_REPO, SIGLIP2_REVISION[:8])
            loop = asyncio.get_running_loop()
            loaded = await loop.run_in_executor(None, loader)
            setattr(self, attr, loaded)
            return loaded

    # -- encoding ---------------------------------------------------------------
    @staticmethod
    def _check(rows: list[list[float]]) -> list[list[float]]:
        for row in rows:
            if len(row) != SIGLIP2_DIM:
                raise RuntimeError(
                    f"image model produced {len(row)}-d vectors, expected {SIGLIP2_DIM}"
                )
        return rows

    def _encode_images(self, vision: Any, processor: Any, batch: list[Any]) -> list[list[float]]:
        import torch

        with torch.inference_mode():
            pixels = processor.image_processor(images=batch, return_tensors="pt")["pixel_values"]
            feats = vision(pixel_values=pixels).pooler_output
            feats = torch.nn.functional.normalize(feats.float(), dim=-1)
        return [[float(x) for x in row] for row in feats.tolist()]

    def _encode_texts(self, text: Any, processor: Any, batch: list[str]) -> list[list[float]]:
        import torch

        with torch.inference_mode():
            # SigLIP was trained on lower-cased text padded to a fixed length.
            tokens = processor.tokenizer(
                [t.lower() for t in batch],
                padding="max_length",
                max_length=SIGLIP2_MAX_TOKENS,
                truncation=True,
                return_tensors="pt",
            )
            feats = text(input_ids=tokens["input_ids"]).pooler_output
            feats = torch.nn.functional.normalize(feats.float(), dim=-1)
        return [[float(x) for x in row] for row in feats.tolist()]

    async def embed_images(self, images: Sequence[bytes]) -> list[list[float]]:
        """One vector per picture. Raises :class:`ImageDecodeError` for a bad file."""

        if not images:
            return []
        loop = asyncio.get_running_loop()
        # Decode everything first: one bad file stops the batch before any model work.
        prepared = [await loop.run_in_executor(None, prepare_image, data) for data in images]
        processor = await self._ensure("_processor", self._load_processor)
        vision = await self._ensure("_vision", self._load_vision)
        out: list[list[float]] = []
        async with self._inference_lock:
            for start in range(0, len(prepared), _BATCH):
                batch = prepared[start : start + _BATCH]
                out += await loop.run_in_executor(
                    None, self._encode_images, vision, processor, batch
                )
        return self._check(out)

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per sentence, in the same space as the pictures."""

        if not texts:
            return []
        loop = asyncio.get_running_loop()
        processor = await self._ensure("_processor", self._load_processor)
        text = await self._ensure("_text", self._load_text)
        cleaned = [t.strip() for t in texts]
        out: list[list[float]] = []
        async with self._inference_lock:
            for start in range(0, len(cleaned), _BATCH):
                batch = cleaned[start : start + _BATCH]
                out += await loop.run_in_executor(None, self._encode_texts, text, processor, batch)
        return self._check(out)


_EMBEDDERS: dict[str, Any] = {}


def get_image_embedder(model_id: str = DEFAULT_IMAGE_MODEL) -> Any:
    """The process-wide embedder for a model (created empty; loads on first use)."""

    embedder = _EMBEDDERS.get(model_id)
    if embedder is None:
        if model_id == "siglip2-base":
            embedder = SiglipImageEmbedder(model_id)
        elif model_id == "bge-vl-base":
            embedder = LocalImageEmbedder(model_id)
        else:
            raise KeyError(f"unknown image model {model_id!r}")
        _EMBEDDERS[model_id] = embedder
    return embedder
