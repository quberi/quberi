"""Keeping the picture a person searched with, so the chat can show it again.

The bytes go to the same object store as documents, under a key of their own
(``{org}/conversations/{conversation}/{picture}``), and a ``conversation_pictures`` row
records whose it is and what it is. Three rules hold throughout:

* **It is a picture or it is not kept.** The format is found by decoding (Pillow), never
  from the upload's label, and only PNG, JPEG, GIF and WebP are accepted. That is also what
  lets the server hand it back with a fixed content type and ``nosniff``.
* **Only its owner can read it.** Being able to see the conversation is not enough.
* **Keeping it is a courtesy to the person, not a condition of the search.** If the object
  store is down the search still answers (see ``send_image_search``); the question is simply
  recorded as having no kept picture.
"""

from __future__ import annotations

import io
import logging
import re
import uuid
from dataclasses import dataclass
from typing import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.conversations.models import ConversationPicture
from quberi_api.services.errors import NotFoundError
from quberi_api.services.documents.connectors import local as local_connector

_log = logging.getLogger(__name__)

_MIME_BY_FORMAT = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}
_FILENAME_MAX = 255
# Control characters and path separators have no business in a display name.
_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f/\\]")


@dataclass(frozen=True)
class PictureFacts:
    """What decoding the bytes showed. ``mime_type`` is never taken from the upload."""

    mime_type: str
    width: int
    height: int


@dataclass(frozen=True)
class StoredPicture:
    """A picture whose bytes are in the object store but whose row is not written yet."""

    picture_id: uuid.UUID
    storage_key: str
    filename: str
    facts: PictureFacts
    size_bytes: int
    checksum_sha256: str


def sniff(data: bytes) -> PictureFacts:
    """Tell what the bytes are. Raises ``ValueError`` if they are not a keepable picture."""

    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as image:
            mime = _MIME_BY_FORMAT.get(image.format or "")
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("not a readable picture") from exc
    if mime is None or width <= 0 or height <= 0:
        raise ValueError("not a picture format that is kept")
    return PictureFacts(mime_type=mime, width=width, height=height)


def clean_filename(name: str | None) -> str:
    """A display name: the base name only, no control characters, at most 255 characters."""

    base = _UNSAFE_NAME.sub("", (name or "").replace("\\", "/").rsplit("/", 1)[-1]).strip()
    return (base[:_FILENAME_MAX]) or "picture"


def storage_key_for(*, organization_id: uuid.UUID, conversation_id: uuid.UUID, picture_id: uuid.UUID) -> str:
    return f"{organization_id}/conversations/{conversation_id}/{picture_id}"


async def put(
    *,
    organization_id: uuid.UUID,
    conversation_id: uuid.UUID,
    data: bytes,
    filename: str | None,
) -> StoredPicture | None:
    """Store the bytes. ``None`` (and a log line) if they cannot be kept; never raises.

    The caller writes the ``ConversationPicture`` row from the result inside its own
    transaction, and calls :func:`discard` if that transaction fails.
    """

    try:
        facts = sniff(data)
        picture_id = uuid.uuid4()
        key = storage_key_for(
            organization_id=organization_id, conversation_id=conversation_id, picture_id=picture_id
        )
        stored = await local_connector.store(
            storage_key=key,
            source_stream=local_connector.wrap_bytes(data),
            content_type=facts.mime_type,
            max_bytes=len(data),
        )
    except Exception:  # noqa: BLE001 - keeping the picture must never fail the search
        _log.warning("could not keep a query picture for conversation %s", conversation_id, exc_info=True)
        return None
    return StoredPicture(
        picture_id=picture_id,
        storage_key=key,
        filename=clean_filename(filename),
        facts=facts,
        size_bytes=stored.size_bytes,
        checksum_sha256=stored.checksum_sha256,
    )


def row_for(
    stored: StoredPicture,
    *,
    organization_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> ConversationPicture:
    return ConversationPicture(
        id=stored.picture_id,
        organization_id=organization_id,
        conversation_id=conversation_id,
        message_id=message_id,
        owner_user_id=owner_user_id,
        filename=stored.filename,
        mime_type=stored.facts.mime_type,
        size_bytes=stored.size_bytes,
        width=stored.facts.width,
        height=stored.facts.height,
        checksum_sha256=stored.checksum_sha256,
        storage_key=stored.storage_key,
    )


async def discard(storage_key: str) -> None:
    """Remove an object best-effort (a failed write, or a picture whose row is gone)."""

    try:
        await local_connector.delete(storage_key)
    except Exception:  # noqa: BLE001
        _log.warning("could not remove object %s", storage_key, exc_info=True)


async def find_for_owner(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> ConversationPicture:
    """The picture of a question, if it is the caller's; otherwise "not found".

    Another member who can see the conversation gets the same answer as for a picture that
    does not exist, so its existence is not revealed.
    """

    row = (
        await session.execute(
            select(ConversationPicture).where(
                ConversationPicture.message_id == message_id,
                ConversationPicture.conversation_id == conversation_id,
                ConversationPicture.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if row is None or row.owner_user_id != user_id:
        raise NotFoundError("picture not found")
    return row


def open_stream(row: ConversationPicture) -> AsyncIterator[bytes]:
    return local_connector.open_stream(row.storage_key)
