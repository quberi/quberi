"""Short-lived links to one page of a document, for outside tools.

A passage found by an outside tool can carry a link to an image of the page it was found on,
so the person can see the original and check the passage against it. The link works like the
picture preview link (``external_image_links``) and for the same reasons: an ``<img>`` in a chat
window cannot send a header, so the link carries its own proof of permission:

    <user id> <grant id> <document id> <version> <page> <expiry>   +   a MAC over those

It names one page of one version for one person for a few minutes, and it is **not trusted when
it is used**: every fetch re-checks that the grant is live, that the person may still read the
space and the document, and that the version is still theirs to see.

The signing key is the same server secret as the picture links, but a **different derived key**
and a different layout, so a page link can never be accepted as a picture link or the other way
round.

Kept short (about 100 characters): a language model has to copy it into its answer unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from quberi_api.core.config import Settings

LINK_TTL_SECONDS = 600
MAX_PAGE = 0xFFFF
MAX_VERSION = 0xFFFF
_MAC_BYTES = 16
_BODY = struct.Struct(">16s16s16sHHI")  # user, grant, document, version, page, expiry
_PURPOSE = b"quberi/external-page-link/v1"


@dataclass(frozen=True)
class PageLink:
    user_id: uuid.UUID
    grant_id: uuid.UUID
    document_id: uuid.UUID
    version_number: int
    page: int
    expires_at: int


def _key(settings: Settings) -> Optional[bytes]:
    secret = settings.external_link_signing_key or settings.encryption_key
    if secret is None:
        return None
    raw = secret.get_secret_value().encode()
    if not raw:
        return None
    return hmac.new(raw, _PURPOSE, hashlib.sha256).digest()


def links_available(settings: Settings) -> bool:
    return _key(settings) is not None


def _mac(key: bytes, body: bytes) -> bytes:
    return hmac.new(key, body, hashlib.sha256).digest()[:_MAC_BYTES]


def sign(
    settings: Settings,
    *,
    user_id: uuid.UUID,
    grant_id: uuid.UUID,
    document_id: uuid.UUID,
    version_number: int,
    page: int,
    now: Optional[float] = None,
    ttl: int = LINK_TTL_SECONDS,
) -> str:
    key = _key(settings)
    if key is None:
        raise RuntimeError("no signing key is configured")
    if not (1 <= version_number <= MAX_VERSION and 1 <= page <= MAX_PAGE):
        raise ValueError("version and page must be between 1 and 65535")
    expires = int((time.time() if now is None else now) + ttl)
    body = _BODY.pack(
        user_id.bytes, grant_id.bytes, document_id.bytes, version_number, page, expires
    )
    return base64.urlsafe_b64encode(body + _mac(key, body)).decode().rstrip("=")


def verify(settings: Settings, token: str, *, now: Optional[float] = None) -> Optional[PageLink]:
    """The link's contents if the MAC is right and it has not expired, else ``None``."""

    key = _key(settings)
    if key is None or not token or len(token) > 200:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, TypeError):
        return None
    if len(raw) != _BODY.size + _MAC_BYTES:
        return None
    body, mac = raw[: _BODY.size], raw[_BODY.size :]
    if not hmac.compare_digest(mac, _mac(key, body)):
        return None
    user, grant, document, version, page, expires = _BODY.unpack(body)
    if expires <= int(time.time() if now is None else now):
        return None
    if version < 1 or page < 1:
        return None
    return PageLink(
        user_id=uuid.UUID(bytes=user),
        grant_id=uuid.UUID(bytes=grant),
        document_id=uuid.UUID(bytes=document),
        version_number=version,
        page=page,
        expires_at=expires,
    )
