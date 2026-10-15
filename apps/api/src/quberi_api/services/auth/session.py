"""Stateless bearer session tokens.

A session is a Fernet-sealed JSON payload carried as
``Authorization: Bearer <token>``. Reusing the platform's existing Fernet
key (same one that seals stored credentials) means no new signing-key to
manage and no session table — the token is self-contained and verified by
decryption + an expiry check.

Trade-off: stateless means we can't revoke a single token before it
expires (short-ish TTL mitigates this). A server-side session store can
be layered in later without changing the call sites, which only see
``issue_session`` / ``read_session``.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Optional

from quberi_api.core.config import get_settings
from quberi_api.services.security import crypto

# Token payload version, so the shape can evolve.
_TOKEN_VERSION = 1


@dataclass(frozen=True)
class SessionClaims:
    identity_id: uuid.UUID
    user_id: uuid.UUID
    organization_id: uuid.UUID
    email: str
    is_guest: bool = False


def issue_session(
    *,
    identity_id: uuid.UUID,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    email: str,
    ttl_seconds: Optional[int] = None,
    is_guest: bool = False,
) -> str:
    """Mint a sealed session token for the resolved user.

    ``is_guest`` marks an anonymous guest session (bound to the demo org).
    Guest sessions are read-only; the API blocks mutating methods for them.
    """

    if ttl_seconds is None:
        ttl_seconds = get_settings().session_ttl_seconds
    now = int(time.time())
    payload = {
        "v": _TOKEN_VERSION,
        "iid": str(identity_id),
        "uid": str(user_id),
        "oid": str(organization_id),
        "email": email,
        "g": bool(is_guest),
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    sealed = crypto.seal(payload)
    # ``seal`` only returns None for empty inputs; our payload is never empty.
    assert sealed is not None  # noqa: S101
    return sealed


def read_session(token: str) -> Optional[SessionClaims]:
    """Validate + decode a session token; ``None`` if invalid/expired."""

    if not token:
        return None
    try:
        payload = crypto.unseal(token)
    except crypto.CryptoError:
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or int(exp) < int(time.time()):
        return None
    try:
        return SessionClaims(
            identity_id=uuid.UUID(str(payload["iid"])),
            user_id=uuid.UUID(str(payload["uid"])),
            organization_id=uuid.UUID(str(payload["oid"])),
            email=str(payload.get("email", "")),
            is_guest=bool(payload.get("g", False)),
        )
    except (KeyError, ValueError):
        return None
