"""Small JPEG previews of picture documents.

Shared by the signed-in thumbnail route and the outside-tool preview link, so both
hand back the same thing: pixels, re-encoded by Pillow, never the stored bytes.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator

THUMBNAIL_SIDE = 320
MAX_SOURCE_BYTES = 26 * 1024 * 1024


class ThumbnailError(Exception):
    """The picture cannot be previewed; ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def render_thumbnail(stream: AsyncIterator[bytes], *, side: int = THUMBNAIL_SIDE) -> bytes:
    """Read a picture from ``stream`` and return a JPEG of at most ``side`` px a side (320).

    Whatever the stored file was (even something that only claims to be an image),
    the result is a plain JPEG, so nothing a browser could run is passed on.
    """

    from quberi_api.services.ai.local_image_embedding import (
        _MAX_SIDE as MODEL_SIDE,
    )
    from quberi_api.services.ai.local_image_embedding import ImageDecodeError, prepare_image

    buf = bytearray()
    async for chunk in stream:
        buf.extend(chunk)
        if len(buf) > MAX_SOURCE_BYTES:
            raise ThumbnailError(413, "picture too large")

    def _render() -> bytes:
        # Decoded no smaller than the picture model's size, and no smaller than asked for, so a
        # larger view (a page image) is really that large and a small one is unchanged.
        image = prepare_image(bytes(buf), max_side=max(side, MODEL_SIDE))
        image.thumbnail((side, side))
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=80)
        return out.getvalue()

    try:
        return await asyncio.to_thread(_render)
    except ImageDecodeError as exc:
        raise ThumbnailError(415, str(exc)) from exc
