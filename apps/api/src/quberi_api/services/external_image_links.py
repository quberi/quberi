"""Short-lived preview links for pictures found by an outside tool.

An outside tool (the Harness plugin) cannot show a picture through an ordinary
bearer-token request: an ``<img>`` in a chat window cannot send a header. So a
picture search hands back a link that carries its own proof of permission:

    <user id> <grant id> <document id> <expiry>   +   a MAC over those, under a server key

The link names one picture for one person for a few minutes. It is not a way in
to anything else, and it is **not trusted when it is used**: every fetch re-checks
that the grant is still live, the person may still read the space, and the
document is still there and not archived. Revoking the tool, losing access or
deleting the picture ends the link at once; the expiry only bounds how long a
copied link stays useful.

The link is kept short (about 90 characters) because a language model has to copy
it into its answer without changing a character.
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
_MAC_BYTES = 16
_BODY = struct.Struct(">16s16s16sI")  # user, grant, document, expiry (unix seconds)
_PURPOSE = b"quberi/external-image-link/v1"


@dataclass(frozen=True)
class ImageLink:
    user_id: uuid.UUID
    grant_id: uuid.UUID
    document_id: uuid.UUID
    expires_at: int


def _key(settings: Settings) -> Optional[bytes]:
    secret = settings.external_link_signing_key or settings.encryption_key
    if secret is None:
        return None
    raw = secret.get_secret_value().encode()
    if not raw:
        return None
    # Derive a key for this purpose only, so the same secret never signs anything else.
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
    now: Optional[float] = None,
    ttl: int = LINK_TTL_SECONDS,
) -> str:
    key = _key(settings)
    if key is None:
        raise RuntimeError("no signing key is configured")
    expires = int((time.time() if now is None else now) + ttl)
    body = _BODY.pack(user_id.bytes, grant_id.bytes, document_id.bytes, expires)
    return base64.urlsafe_b64encode(body + _mac(key, body)).decode().rstrip("=")


def verify(settings: Settings, token: str, *, now: Optional[float] = None) -> Optional[ImageLink]:
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
    user, grant, document, expires = _BODY.unpack(body)
    if expires <= int(time.time() if now is None else now):
        return None
    return ImageLink(
        user_id=uuid.UUID(bytes=user),
        grant_id=uuid.UUID(bytes=grant),
        document_id=uuid.UUID(bytes=document),
        expires_at=expires,
    )
