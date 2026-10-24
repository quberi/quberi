"""An image of one page of a document, for the page links handed to outside tools.

A PDF page is drawn by a child process (see ``page_render_child``) with a time limit; a picture
is re-encoded by Pillow at a larger size than its search preview. Either way the result is a
plain JPEG made from pixels, never the stored bytes, so nothing a browser could run is passed on.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from quberi_api.services.documents.thumbnail import (
    MAX_SOURCE_BYTES,
    ThumbnailError,
    render_thumbnail,
)

PAGE_WIDTH = 1100
PICTURE_SIDE = 1100
RENDER_TIMEOUT_SECONDS = 25
MAX_RESULT_BYTES = 4 * 1024 * 1024
# A few pages at once at most: each one is a process of its own with real memory.
_slots = asyncio.Semaphore(2)

_NO_SUCH_PAGE = 2
_CHILD = Path(__file__).with_name("page_render_child.py")


async def render_pdf_page(
    stream: AsyncIterator[bytes], *, page: int, width: int = PAGE_WIDTH
) -> bytes:
    """A JPEG of page ``page`` (1-based) of the PDF in ``stream``."""

    buf = bytearray()
    async for chunk in stream:
        buf.extend(chunk)
        if len(buf) > MAX_SOURCE_BYTES:
            raise ThumbnailError(413, "file too large")
    async with _slots:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            # By path, not ``-m``: importing the package would load the whole application
            # (database models, settings) into a process that only has to draw a page.
            str(_CHILD),
            str(page),
            str(width),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(
                process.communicate(bytes(buf)), timeout=RENDER_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise ThumbnailError(504, "the page took too long to draw") from None
    if process.returncode == _NO_SUCH_PAGE:
        raise ThumbnailError(404, "no such page")
    if process.returncode != 0 or not out or len(out) > MAX_RESULT_BYTES:
        raise ThumbnailError(415, "the page cannot be drawn")
    return out


async def render_picture(stream: AsyncIterator[bytes], *, side: int = PICTURE_SIDE) -> bytes:
    """A JPEG of a picture, at most ``side`` px a side (default larger than its search preview)."""

    return await render_thumbnail(stream, side=side)
