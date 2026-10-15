"""Feishu event-subscription (webhook) decoding.

Feishu posts events to our callback URL. Two wrinkles:

1. **Encryption** — if an *Encrypt Key* is set in the console, the body
   arrives as ``{"encrypt": "<base64>"}``. Decrypt with AES-256-CBC
   where ``key = sha256(encrypt_key)`` and the IV is the first 16 bytes
   of the decoded ciphertext.
2. **URL verification** — on first save, Feishu sends
   ``{"type": "url_verification", "challenge": "...", "token": "..."}``
   and expects ``{"challenge": "..."}`` echoed back.

Event bodies (v2 schema) look like::

    {
      "schema": "2.0",
      "header": {
        "event_type": "drive.file.edit_v1",
        "tenant_key": "...",
        "token": "<verification_token>"
      },
      "event": { ...file-specific... }
    }
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from quberi_api.core.config import get_settings

_log = logging.getLogger(__name__)


class EventVerificationError(RuntimeError):
    """Raised when an inbound event fails token / decrypt validation."""


@dataclass(frozen=True)
class DecodedEvent:
    """A decoded webhook payload."""

    raw: dict
    is_url_verification: bool
    challenge: Optional[str]
    event_type: Optional[str]
    tenant_key: Optional[str]


def _decrypt(encrypt_b64: str, encrypt_key: str) -> dict:
    """AES-256-CBC decrypt per Feishu's scheme."""

    # Deferred import — cryptography is already a dep (Fernet), reuse it.
    from cryptography.hazmat.primitives.ciphers import (
        Cipher,
        algorithms,
        modes,
    )

    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    blob = base64.b64decode(encrypt_b64)
    iv, ciphertext = blob[:16], blob[16:]
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    # Strip PKCS7 padding.
    pad_len = padded[-1]
    plaintext = padded[:-pad_len]
    return json.loads(plaintext.decode("utf-8"))


def decode_event(body: dict) -> DecodedEvent:
    """Decode + validate an inbound webhook body.

    Raises :class:`EventVerificationError` if the verification token
    doesn't match (when configured).
    """

    settings = get_settings()
    encrypt_key = (
        settings.feishu_encrypt_key.get_secret_value()
        if settings.feishu_encrypt_key
        else None
    )
    verification_token = (
        settings.feishu_verification_token.get_secret_value()
        if settings.feishu_verification_token
        else None
    )

    # 1. Decrypt if needed.
    if "encrypt" in body:
        if not encrypt_key:
            raise EventVerificationError(
                "received encrypted event but QUBERI_FEISHU_ENCRYPT_KEY is unset"
            )
        payload = _decrypt(body["encrypt"], encrypt_key)
    else:
        payload = body

    # 2. URL verification handshake.
    if payload.get("type") == "url_verification":
        token = payload.get("token")
        if verification_token and token != verification_token:
            raise EventVerificationError("url_verification token mismatch")
        return DecodedEvent(
            raw=payload,
            is_url_verification=True,
            challenge=payload.get("challenge"),
            event_type=None,
            tenant_key=None,
        )

    # 3. Regular event — validate token in header (v2) or top-level (v1).
    header = payload.get("header") or {}
    token = header.get("token") or payload.get("token")
    if verification_token and token and token != verification_token:
        raise EventVerificationError("event verification token mismatch")

    event_type = header.get("event_type") or payload.get("event", {}).get("type")
    tenant_key = header.get("tenant_key")
    return DecodedEvent(
        raw=payload,
        is_url_verification=False,
        challenge=None,
        event_type=event_type,
        tenant_key=tenant_key,
    )


# ---------------------------------------------------------------------------
# Event → affected file tokens
# ---------------------------------------------------------------------------


# Drive/docs file events we care about. Feishu emits several granular
# types; we treat any of them as "the folder changed, re-sync it".
_FILE_EVENT_PREFIXES = (
    "drive.file.",  # created_in_folder / edit / title_updated / trashed
    "docx.document.",
)


def is_file_event(event_type: Optional[str]) -> bool:
    if not event_type:
        return False
    return any(event_type.startswith(p) for p in _FILE_EVENT_PREFIXES)


def extract_file_tokens(payload: dict) -> dict[str, Any]:
    """Best-effort pull of file/folder tokens from a drive event body.

    Feishu's per-event shapes vary; we grab whatever identifying tokens
    are present so the handler can match them against registered sources.
    """

    event = payload.get("event") or {}
    file_token = (
        event.get("file_token")
        or event.get("token")
        or (event.get("file") or {}).get("token")
    )
    folder_token = (
        event.get("folder_token")
        or event.get("parent_token")
        or (event.get("file") or {}).get("parent_token")
    )
    return {"file_token": file_token, "folder_token": folder_token}
