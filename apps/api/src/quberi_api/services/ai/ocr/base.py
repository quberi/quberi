"""The OCR contract: what any engine hands back, and how it can fail.

Every caller (picture indexing, scanned-PDF pages) depends only on this module.
Which engine sits behind it (a model run in a child process on this host, or one
day a hosted service) is a deployment choice, the same way the chat model is.

Coordinates are always in pixels of the picture **as it was given**, even when the
engine shrank it first, so a box can later be drawn on the original.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

Point = tuple[float, float]
Polygon = tuple[Point, Point, Point, Point]


class OcrError(RuntimeError):
    """OCR did not produce a result. The message says why, in plain words."""


class OcrImageError(OcrError):
    """The bytes are not a picture we can safely read. Retrying will not help."""


class OcrTimeoutError(OcrError):
    """The engine took too long and was stopped."""


class OcrUnavailableError(OcrError):
    """The engine could not run (failed to start, or was stopped for using too much memory)."""


@dataclass(frozen=True)
class OcrLine:
    """One line of text the engine found.

    ``polygon`` is the four corners, clockwise from the top-left of the text as
    written (so a slanted line has a slanted box).
    """

    text: str
    confidence: float
    polygon: Polygon

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        xs = [p[0] for p in self.polygon]
        ys = [p[1] for p in self.polygon]
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass(frozen=True)
class OcrResult:
    """Everything found in one picture, in reading order (top to bottom, then left to right).

    ``engine``/``model``/``version`` identify what produced the text; together with the
    picture's checksum they are the key that says "this picture is already done".
    ``dropped`` counts lines left out because of the per-picture line cap, so a caller can
    say the result is partial.
    """

    lines: list[OcrLine]
    engine: str
    model: str
    version: str
    width: int
    height: int
    dropped: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@runtime_checkable
class OCRProvider(Protocol):
    """Anything that can read the text in a picture."""

    name: str

    async def recognize(self, image: bytes) -> OcrResult:
        """Read ``image`` (PNG, JPEG, GIF or WebP bytes). Raises :class:`OcrError`."""
        ...

    async def aclose(self) -> None:
        """Release whatever the engine holds (a child process, loaded models)."""
        ...
