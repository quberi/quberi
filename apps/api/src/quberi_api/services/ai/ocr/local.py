"""OCR on this host: PP-OCRv5 mobile through ``transformers``, in a child process."""

from __future__ import annotations

import os
from typing import Any

from .base import OcrImageError, OcrLine, OcrResult
from .models import ENGINE_NAME, ENGINE_VERSION, MODEL_NAME, EngineConfig
from .runner import ProcessRunner
from .worker import main as worker_main

# A picture bigger than this is refused before it is sent anywhere. Pixels are limited
# separately inside the child; this stops a huge upload from being copied through a pipe.
MAX_PICTURE_BYTES = 32 * 1024 * 1024


class LocalOCRProvider:
    """Reads text with a local model. Nothing leaves this machine."""

    name = "local"

    def __init__(
        self,
        config: EngineConfig | None = None,
        *,
        max_memory_mb: int = 2048,
        request_timeout_s: float = 60.0,
        startup_timeout_s: float = 300.0,
        idle_unload_s: float = 300.0,
        runner: Any | None = None,
    ) -> None:
        self.config = (config or EngineConfig(hf_home=os.getenv("HF_HOME"))).clamped()
        # ``runner`` may be injected (tests pass a stand-in that never starts a process).
        self._runner = runner or ProcessRunner(
            target=worker_main,
            target_args=(self.config,),
            max_memory_mb=max_memory_mb,
            request_timeout_s=request_timeout_s,
            startup_timeout_s=startup_timeout_s,
            idle_unload_s=idle_unload_s,
        )

    async def recognize(self, image: bytes) -> OcrResult:
        if not image:
            raise OcrImageError("the image is empty")
        if len(image) > MAX_PICTURE_BYTES:
            raise OcrImageError("the picture file is too large")
        raw = await self._runner.call(image)
        return OcrResult(
            lines=[
                OcrLine(text=text, confidence=float(conf), polygon=tuple(map(tuple, poly)))  # type: ignore[arg-type]
                for text, conf, poly in raw["lines"]
            ],
            engine=ENGINE_NAME,
            model=MODEL_NAME,
            version=ENGINE_VERSION,
            width=int(raw["width"]),
            height=int(raw["height"]),
            dropped=int(raw.get("dropped", 0)),
            notes=list(raw.get("notes", [])),
        )

    async def aclose(self) -> None:
        await self._runner.aclose()
