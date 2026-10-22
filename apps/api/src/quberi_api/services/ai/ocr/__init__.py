"""Reading text out of pictures.

Callers import from here and depend only on :class:`OCRProvider`. The engine behind it is
a deployment choice (see ``factory.get_ocr_provider``).
"""

from .base import (
    OcrError,
    OcrImageError,
    OcrLine,
    OCRProvider,
    OcrResult,
    OcrTimeoutError,
    OcrUnavailableError,
)
from .factory import close_ocr_provider, get_ocr_provider

__all__ = [
    "OCRProvider",
    "OcrError",
    "OcrImageError",
    "OcrLine",
    "OcrResult",
    "OcrTimeoutError",
    "OcrUnavailableError",
    "close_ocr_provider",
    "get_ocr_provider",
]
