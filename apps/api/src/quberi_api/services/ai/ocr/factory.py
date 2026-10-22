"""Builds the process-wide OCR provider from server settings."""

from __future__ import annotations

import os

from quberi_api.core.config import get_settings

from .base import OCRProvider, OcrUnavailableError
from .local import LocalOCRProvider
from .models import EngineConfig

_PROVIDER: OCRProvider | None = None


def get_ocr_provider() -> OCRProvider:
    """The shared provider (created empty; the child process starts on first use).

    Raises :class:`OcrUnavailableError` when OCR is switched off or the configured
    provider is not one this build knows, so callers can mark a picture "skipped"
    rather than "failed".
    """

    global _PROVIDER
    settings = get_settings()
    if not settings.image_ocr_enabled:
        raise OcrUnavailableError("OCR is turned off on this server")
    if _PROVIDER is not None:
        return _PROVIDER
    if settings.ocr_provider != "local":
        raise OcrUnavailableError(f"unknown OCR provider {settings.ocr_provider!r}")
    _PROVIDER = LocalOCRProvider(
        EngineConfig(
            det_side=settings.ocr_det_side,
            min_confidence=settings.ocr_min_confidence,
            max_lines=settings.ocr_max_lines,
            threads=settings.ocr_threads,
            hf_home=os.getenv("HF_HOME"),
        ),
        max_memory_mb=settings.ocr_max_memory_mb,
        request_timeout_s=settings.ocr_timeout_s,
        startup_timeout_s=settings.ocr_startup_timeout_s,
        idle_unload_s=settings.ocr_idle_unload_s,
    )
    return _PROVIDER


async def close_ocr_provider() -> None:
    """Stop the child process (called on shutdown and by tests)."""

    global _PROVIDER
    provider, _PROVIDER = _PROVIDER, None
    if provider is not None:
        await provider.aclose()
