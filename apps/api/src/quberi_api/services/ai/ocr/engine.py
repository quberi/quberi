"""The OCR pipeline itself: find text boxes, straighten each one, read it.

This module imports torch, torchvision and OpenCV. It is only ever imported inside
the child process (see ``worker.py``); the API and worker processes never load it.

The output is plain Python values so it can cross a pipe.
"""

from __future__ import annotations

import hashlib
import io
import logging
from typing import Any

from .base import OcrImageError, OcrLine
from .geometry import cap_lines, fit_within, order_polygon, reading_order, upright_size
from .models import (
    DET_REPO,
    DET_REVISION,
    DET_SHA256,
    REC_REPO,
    REC_REVISION,
    REC_SHA256,
    EngineConfig,
)

_log = logging.getLogger(__name__)

_ACCEPTED_FORMATS = ("PNG", "JPEG", "GIF", "WEBP")
# A box smaller than this on either side cannot hold a readable character.
_MIN_CROP_SIDE = 3


def decode_image(data: bytes, max_pixels: int) -> Any:
    """Untrusted bytes to an RGB Pillow image, or :class:`OcrImageError`.

    Same rules as picture search: format decided by what Pillow detects, a pixel limit
    against decompression bombs, first frame only for animations. Nothing is written
    anywhere. Unlike picture search the image is **not** shrunk to a thumbnail: small
    print needs the real pixels.
    """

    from PIL import Image, UnidentifiedImageError

    if not data:
        raise OcrImageError("the image is empty")
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        image = Image.open(io.BytesIO(data))
        if image.format not in _ACCEPTED_FORMATS:
            raise OcrImageError("only PNG, JPEG, GIF and WebP pictures are accepted")
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > max_pixels:
            raise OcrImageError("the picture has too many pixels")
        image.load()
    except OcrImageError:
        raise
    except Image.DecompressionBombError as exc:
        raise OcrImageError("the picture has too many pixels") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise OcrImageError("the file is not a readable picture") from exc
    return image.convert("RGB")


def _verify_weights(repo: str, revision: str, expected_sha256: str, cache_dir: str | None) -> None:
    from huggingface_hub import hf_hub_download

    path = hf_hub_download(repo, "model.safetensors", revision=revision, cache_dir=cache_dir)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != expected_sha256:
        raise RuntimeError(
            f"{repo}@{revision[:8]} weights do not match the audited hash; refusing to use them"
        )


class OcrEngine:
    """Holds the two loaded models. Build with :meth:`load`; call :meth:`run` per picture."""

    def __init__(self, config: EngineConfig, det: Any, det_proc: Any, rec: Any, rec_proc: Any):
        self.config = config
        self._det = det
        self._det_proc = det_proc
        self._rec = rec
        self._rec_proc = rec_proc

    @classmethod
    def load(cls, config: EngineConfig) -> OcrEngine:
        import torch
        from transformers import (
            AutoImageProcessor,
            AutoModelForObjectDetection,
            AutoModelForTextRecognition,
        )

        config = config.clamped()
        torch.set_num_threads(config.threads)
        cache = config.hf_home
        _verify_weights(DET_REPO, DET_REVISION, DET_SHA256, cache)
        _verify_weights(REC_REPO, REC_REVISION, REC_SHA256, cache)
        common = {"revision": None, "cache_dir": cache, "trust_remote_code": False}
        det = AutoModelForObjectDetection.from_pretrained(
            DET_REPO, **{**common, "revision": DET_REVISION}, use_safetensors=True
        ).eval()
        det_proc = AutoImageProcessor.from_pretrained(
            DET_REPO, **{**common, "revision": DET_REVISION}
        )
        rec = AutoModelForTextRecognition.from_pretrained(
            REC_REPO, **{**common, "revision": REC_REVISION}, use_safetensors=True
        ).eval()
        rec_proc = AutoImageProcessor.from_pretrained(
            REC_REPO, **{**common, "revision": REC_REVISION}
        )
        return cls(config, det, det_proc, rec, rec_proc)

    # -- one picture -------------------------------------------------------------
    def run(self, data: bytes) -> dict[str, Any]:
        """Read one picture. Returns a dict of plain values (see ``local.py``)."""

        import numpy as np

        cfg = self.config
        image = decode_image(data, cfg.max_pixels)
        width, height = image.size
        pixels = np.asarray(image)

        polygons = self._detect(image, width, height)
        crops = [self._straighten(pixels, polygon) for polygon in polygons]
        tall = [upright_size(polygon)[2] for polygon in polygons]
        texts = self._read_oriented(crops, tall)

        notes: list[str] = []
        lines: list[OcrLine] = []
        low_confidence = 0
        for polygon, (text, score) in zip(polygons, texts, strict=True):
            text = text.strip()
            if not text:
                continue
            if score < cfg.min_confidence:
                low_confidence += 1
                continue
            lines.append(OcrLine(text=text, confidence=float(score), polygon=polygon))
        if low_confidence:
            notes.append(f"{low_confidence} low-confidence line(s) left out")

        kept, dropped = cap_lines(reading_order(lines), cfg.max_lines)
        if dropped:
            notes.append(f"{dropped} line(s) over the {cfg.max_lines}-line cap left out")
        return {
            "lines": [(ln.text, ln.confidence, ln.polygon) for ln in kept],
            "width": width,
            "height": height,
            "dropped": dropped,
            "notes": notes,
        }

    # -- stages -------------------------------------------------------------------
    def _detect(self, image: Any, width: int, height: int) -> list[Any]:
        import torch

        side = fit_within(width, height, self.config.det_side)
        with torch.inference_mode():
            inputs = self._det_proc(
                images=image, return_tensors="pt", limit_side_len=max(side)
            )
            outputs = self._det(**inputs)
            result = self._det_proc.post_process_object_detection(
                outputs, target_sizes=inputs["target_sizes"]
            )[0]
        polygons = []
        for box in result["boxes"]:
            points = [(float(x), float(y)) for x, y in box.tolist()]
            if len(points) != 4:
                continue
            clamped = [
                (min(max(x, 0.0), width - 1.0), min(max(y, 0.0), height - 1.0)) for x, y in points
            ]
            polygons.append(order_polygon(clamped))
        return polygons

    @staticmethod
    def _straighten(pixels: Any, polygon: Any) -> Any | None:
        """Warp one (possibly slanted) text box into an upright strip of pixels."""

        import cv2
        import numpy as np

        crop_w, crop_h, rotate = upright_size(polygon)
        if crop_w < _MIN_CROP_SIDE or crop_h < _MIN_CROP_SIDE:
            return None
        source = np.array(polygon, dtype=np.float32)
        target = np.array(
            [[0, 0], [crop_w, 0], [crop_w, crop_h], [0, crop_h]], dtype=np.float32
        )
        matrix = cv2.getPerspectiveTransform(source, target)
        strip = cv2.warpPerspective(
            pixels,
            matrix,
            (crop_w, crop_h),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
        # Tall boxes (vertical writing, turned notes) have to be turned a quarter turn
        # to read left to right, but which way depends on how the text was turned and
        # a box cannot say. ``_read_oriented`` tries both.
        return np.rot90(strip) if rotate else strip

    def _read_oriented(
        self, crops: list[Any | None], tall: list[bool]
    ) -> list[tuple[str, float]]:
        """Read every crop; for tall ones also read the opposite turn and keep the better.

        On the evaluation set the wrong way round gave confidence 0.2 to 0.6 and the right
        way 0.97 to 1.00, but some wrong readings still scored 0.8, so the choice is "the
        higher of the two", never a threshold on one reading. Only tall crops pay for a
        second read.
        """

        import numpy as np

        first = self._read(crops)
        retry = {i for i, is_tall in enumerate(tall) if is_tall and crops[i] is not None}
        if not retry:
            return first
        second = self._read(
            [np.rot90(crop, 2) if i in retry else None for i, crop in enumerate(crops)]
        )
        return [
            second[i] if i in retry and second[i][1] > first[i][1] else first[i]
            for i in range(len(crops))
        ]

    def _read(self, crops: list[Any | None]) -> list[tuple[str, float]]:
        import torch
        from PIL import Image

        out: list[tuple[str, float]] = [("", 0.0)] * len(crops)
        indices = [i for i, crop in enumerate(crops) if crop is not None]
        # Similar widths batch together with less padding.
        indices.sort(key=lambda i: crops[i].shape[1])
        batch = self.config.rec_batch
        for start in range(0, len(indices), batch):
            chunk = indices[start : start + batch]
            images = [Image.fromarray(crops[i]) for i in chunk]
            with torch.inference_mode():
                inputs = self._rec_proc(images=images, return_tensors="pt")
                decoded = self._rec_proc.post_process_text_recognition(self._rec(**inputs))
            for i, item in zip(chunk, decoded, strict=True):
                out[i] = (str(item["text"]), float(item["score"]))
        return out
