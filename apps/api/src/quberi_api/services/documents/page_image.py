"""Open one version of a document and draw one page of it as a JPEG.

Two callers need exactly this and must not drift apart: the signed page links an outside tool is
handed (``api/v1/external.py``) and the ``get_document_image`` MCP tool. Both open the version
with the same permission check as a signed-in download, so a person who may not download it gets
nothing, and both draw it with the same child-process renderer (``page_render``).
"""

from __future__ import annotations

import io
import uuid
from typing import Optional

from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.documents.models import Document
from quberi_api.services.documents import core as documents_core
from quberi_api.services.documents import new_version as new_version_service
from quberi_api.services.documents import page_render
from quberi_api.services.documents.thumbnail import ThumbnailError
from quberi_api.services.errors import NotFoundError
from quberi_api.services.rag.indexer import is_picture

# What an image handed straight to a model should weigh at most, once encoded.
DEFAULT_BYTE_CAP = 200 * 1024
MIN_WIDTH = 256
_QUALITIES = (85, 75, 65, 55, 45)


class NoPageImageError(Exception):
    """The file is neither a PDF nor a picture, so there is no page of it to draw."""


async def render_document_page(
    session: AsyncSession,
    *,
    document: Document,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    version_number: int,
    page: int,
    width: int = page_render.PAGE_WIDTH,
) -> bytes:
    """A JPEG of ``page`` of ``version_number`` of ``document``, opened for ``user_id``.

    Raises ``NotFoundError`` / ``DocumentPermissionError`` when the person may not read it,
    ``NoPageImageError`` for a kind of file that cannot be drawn, and ``ThumbnailError`` when
    drawing fails (404 for a page that does not exist).
    """

    legacy = document.current_version_id is None and version_number == 1
    try:
        # The same permission check as a signed-in download of this version.
        version, stream = await new_version_service.open_version_stream(
            session,
            document_id=document.id,
            version_number=version_number,
            organization_id=organization_id,
            space_id=document.space_id,
            caller_user_id=user_id,
        )
        mime, filename = (version.mime_type or "").lower(), version.filename
    except NotFoundError:
        if not legacy:
            raise
        # A file stored before versions were recorded has no version row: its one version is
        # the file itself, opened with the same check as a download.
        opened, stream = await documents_core.open_download_stream(
            session,
            document_id=document.id,
            organization_id=organization_id,
            space_id=document.space_id,
            caller_user_id=user_id,
        )
        mime, filename = (opened.mime_type or "").lower(), opened.name
    if mime == "application/pdf" or filename.lower().endswith(".pdf"):
        return await page_render.render_pdf_page(stream, page=page, width=width)
    if is_picture(mime):
        if page != 1:
            # A picture is one page: asking for another is asking for a page that is not there.
            raise ThumbnailError(404, "no such page")
        return await page_render.render_picture(stream, side=width)
    raise NoPageImageError(mime or filename)


def fit_jpeg(
    data: bytes, *, max_width: int, byte_cap: int = DEFAULT_BYTE_CAP
) -> tuple[bytes, int, int]:
    """Re-encode a JPEG so it is at most ``max_width`` wide and, if it can be, ``byte_cap`` bytes.

    Lowers the quality in steps first, then the width, never below ``MIN_WIDTH``. The smallest
    attempt is returned if even that is over the cap (a page of dense noise at 256 px can be).
    Returns ``(jpeg bytes, width, height)``.
    """

    with Image.open(io.BytesIO(data)) as opened:
        image = opened.convert("RGB")
    width = min(image.width, max_width)
    best: Optional[tuple[bytes, int, int]] = None
    while True:
        height = max(1, round(image.height * width / image.width))
        scaled = image if width == image.width else image.resize((width, height), Image.LANCZOS)
        for quality in _QUALITIES:
            buf = io.BytesIO()
            scaled.save(buf, format="JPEG", quality=quality, optimize=True)
            encoded = buf.getvalue()
            best = (encoded, width, height)
            if len(encoded) <= byte_cap:
                return best
        if width <= MIN_WIDTH:
            return best
        width = max(MIN_WIDTH, int(width * 0.85))
