"""Stateless bearer session tokens.

A session is a Fernet-sealed JSON payload carried as
``Authorization: Bearer <token>``. Reusing the platform's existing Fernet
key (same one that seals stored credentials) means no new signing-key to
manage and no session table — the token is self-contained and verified by
decryption + an expiry check.

A token carries the user's ``session_version`` (``sv``). Bumping
``users.session_version`` invalidates every token that user holds at once (used
when a member is deactivated). A single token can also be revoked on sign-out:
its sha256 goes into ``revoked_sessions`` until it would have expired anyway.
Both checks happen in ``api.security.require_caller``.

Production code mints tokens through :func:`mint_session`, which refuses
accounts that may not sign in; ``issue_session`` is the low-level signer.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.core.config import get_settings
from quberi_api.domain.enums import BLOCKED_USER_STATUSES
from quberi_api.domain.identity.models import Organization, User
from quberi_api.services.auth.account_status import (
    ACCOUNT_UNAVAILABLE_MESSAGE,
    AccountUnavailableError,
)
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
    # Tokens issued before session versions existed carry no ``sv``; they read as 0.
    session_version: int = 0
    # Unix time the token stops being valid (the ``exp`` claim).
    expires_at: int = 0


def issue_session(
    *,
    identity_id: uuid.UUID,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    email: str,
    ttl_seconds: Optional[int] = None,
    is_guest: bool = False,
    session_version: int = 0,
) -> str:
    """Sign a session token for the resolved user (no account checks).

    Application code should call :func:`mint_session` instead.

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
        "sv": int(session_version),
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
    version = payload.get("sv", 0)
    if isinstance(version, bool) or not isinstance(version, int):
        return None
    try:
        return SessionClaims(
            identity_id=uuid.UUID(str(payload["iid"])),
            user_id=uuid.UUID(str(payload["uid"])),
            organization_id=uuid.UUID(str(payload["oid"])),
            email=str(payload.get("email", "")),
            is_guest=bool(payload.get("g", False)),
            session_version=version,
            expires_at=int(exp),
        )
    except (KeyError, ValueError):
        return None


async def mint_session(
    db: AsyncSession,
    *,
    identity_id: uuid.UUID,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    email: str,
    is_guest: bool = False,
) -> str:
    """Sign a session for ``user_id`` after checking the account may sign in.

    This is the last gate on every sign-in path: whichever flow resolved the
    account, a suspended or archived user, or an organization that is not active,
    never receives a token. The token carries the user's current session version.
    """

    row = (
        await db.execute(
            select(User.status, User.session_version, Organization.status)
            .join(Organization, Organization.id == User.organization_id)
            .where(User.id == user_id, Organization.id == organization_id)
        )
    ).first()
    if (
        row is None
        or row[0] in BLOCKED_USER_STATUSES
        or row[2] != "active"
    ):
        raise AccountUnavailableError(ACCOUNT_UNAVAILABLE_MESSAGE)
    return issue_session(
        identity_id=identity_id,
        user_id=user_id,
        organization_id=organization_id,
        email=email,
        is_guest=is_guest,
        session_version=int(row[1]),
    )


def session_token_hash(token: str) -> str:
    """Stable fingerprint of a token (the token itself is never stored)."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# One statement with an existence check: if the user row is already gone nothing is
# inserted and the surrounding transaction stays usable (a failed foreign key would
# leave it aborted).
_REVOKE_SQL = text(
    "INSERT INTO revoked_sessions (token_hash, user_id, expires_at) "
    "SELECT :h, :uid, :exp WHERE EXISTS (SELECT 1 FROM users WHERE id = :uid) "
    "ON CONFLICT DO NOTHING"
)


async def revoke_session(db: AsyncSession, token: str) -> None:
    """Sign out one session. Idempotent; invalid or unknown tokens are ignored.

    The caller commits.
    """

    claims = read_session(token)
    if claims is None or claims.is_guest:
        return
    await db.execute(
        _REVOKE_SQL,
        {
            "h": session_token_hash(token),
            "uid": claims.user_id,
            "exp": datetime.fromtimestamp(claims.expires_at, tz=timezone.utc),
        },
    )
