"""Fernet-based sealing for credentials-at-rest.

Every secret we persist (LLM API keys, connector credentials) goes
through :func:`seal` on write and :func:`unseal` on read. The master
key is read from ``QUBERI_ENCRYPTION_KEY`` at process start.

Format
------
``seal`` returns a *string* prefixed with a version tag so we can rotate
the underlying scheme without a data migration::

    fernet1:<urlsafe-base64 ciphertext>

Legacy Slice-E rows carry ``b64:<...>`` — a base64-only placeholder that
was never actual encryption. ``unseal`` transparently reads both formats
so the migration path is "read old, re-seal new" without a downgrade
window. When we drop b64 support the code below is the single place to
change.

Key rotation
------------
Rotating ``QUBERI_ENCRYPTION_KEY`` requires re-sealing every stored
secret. Support for a *key set* (multiple decrypt keys + one encrypt
key) can be added later by turning ``_get_fernet()`` into a
``MultiFernet``; today one key is fine because we haven't shipped
anything encrypted yet.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken


class CryptoError(RuntimeError):
    """Base error class for crypto operations."""


class KeyMissing(CryptoError):
    """Raised when ``QUBERI_ENCRYPTION_KEY`` is missing or malformed."""


_FERNET_PREFIX = "fernet1:"
_LEGACY_B64_PREFIX = "b64:"

# Populated lazily on first use so tests can override the env var before
# import and the API stays importable in environments where the key is
# not yet configured (e.g. running ``alembic`` against a plain postgres).
_fernet: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet
    raw = os.getenv("QUBERI_ENCRYPTION_KEY")
    if not raw:
        raise KeyMissing(
            "QUBERI_ENCRYPTION_KEY is not set; refusing to (un)seal secrets"
        )
    try:
        _fernet = Fernet(raw.encode() if isinstance(raw, str) else raw)
    except (ValueError, TypeError) as exc:
        raise KeyMissing(
            "QUBERI_ENCRYPTION_KEY is not a valid Fernet key "
            "(urlsafe base64, 32 raw bytes)"
        ) from exc
    return _fernet


def seal(value: Any) -> Optional[str]:
    """Encrypt a JSON-serialisable value.

    ``None`` and the empty string / dict pass through as ``None`` — the
    caller shouldn't have to distinguish "no credentials" from "empty
    credentials" downstream.
    """

    if value is None:
        return None
    if isinstance(value, (dict, list)) and not value:
        return None
    if isinstance(value, str) and not value:
        return None
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ciphertext = _get_fernet().encrypt(payload)
    return _FERNET_PREFIX + ciphertext.decode("ascii")


def unseal(sealed: Optional[str]) -> Any:
    """Decrypt a value previously produced by :func:`seal`.

    Returns ``None`` if the input is ``None`` / empty. Legacy Slice-E
    rows sealed with ``b64:<...>`` are still accepted so we don't need
    a data migration before switching to real Fernet.
    """

    if sealed is None or sealed == "":
        return None
    if sealed.startswith(_FERNET_PREFIX):
        blob = sealed[len(_FERNET_PREFIX):].encode("ascii")
        try:
            plaintext = _get_fernet().decrypt(blob)
        except InvalidToken as exc:
            raise CryptoError("stored ciphertext failed decryption") from exc
        return json.loads(plaintext.decode("utf-8"))
    if sealed.startswith(_LEGACY_B64_PREFIX):
        blob = sealed[len(_LEGACY_B64_PREFIX):]
        try:
            payload = base64.b64decode(blob)
        except (ValueError, TypeError) as exc:
            raise CryptoError("legacy b64 sealed value is corrupt") from exc
        return json.loads(payload.decode("utf-8"))
    raise CryptoError(f"unknown sealed prefix: {sealed[:16]!r}")


def is_configured() -> bool:
    """Cheap check used by health probes / bootstrap; never raises."""

    try:
        _get_fernet()
        return True
    except KeyMissing:
        return False
