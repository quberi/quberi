"""Platform operator accounts: passwords, sign-in, sessions.

Design notes
------------
* **No default password.** At first boot, if no account exists, one is created
  from ``QUBERI_BOOTSTRAP_ADMIN_*``. With no password configured a random one is
  generated, logged once, and the account must change it before doing anything
  else. A password the operator set deliberately is not forced to change.
* **Slow salted hash** (scrypt, via ``cryptography`` which the project already
  depends on). The standard library's ``hashlib.scrypt`` is not used because it
  is missing from Pythons linked against LibreSSL (macOS system Python).
  The parameters travel with each hash, so they can be raised later without a
  migration, and are bounded when read so a damaged row cannot make a login
  allocate unbounded memory.
* **Sign-in cannot be hammered.** Failures are counted per account and per
  client address in Redis; once over the limit the answer is 429 regardless of
  whether the password is right. If Redis is down, sign-in is refused (fail
  closed) rather than becoming unlimited guessing.
* **No account enumeration.** An unknown username costs the same hashing work
  and returns the same message as a wrong password.
* **Sessions** are sealed with the platform key like member sessions, but carry
  a different ``typ`` and are signed over the account's ``token_version``: a
  member session is never an admin session, and changing the password ends
  every older admin session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.core.config import Settings
from quberi_api.domain.platform_admin.models import PlatformAdmin
from quberi_api.services.admin_messages import render
from quberi_api.services.security import crypto

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

# scrypt memory use is ~128 * N * r bytes: N=2**15, r=8 is ~32 MiB per hash.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
# Bounds applied when *reading* a stored hash.
_MAX_N = 2**17
_MAX_R = 16
_MAX_P = 4
_SALT_BYTES = 16
_DK_LEN = 32

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128


class PlatformAdminError(Exception):
    """A refused request; ``status`` is the HTTP status to answer with."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 400,
        code: Optional[str] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        # Stable message code for the admin console (see ``admin_messages``); ``message``
        # stays the English text.
        self.code = code
        self.params = params or {}


def _coded(code: str, status: int = 400, **params: Any) -> PlatformAdminError:
    return PlatformAdminError(
        render(code, "en", params), status=status, code=code, params=params
    )


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = Scrypt(
        salt=salt, length=_DK_LEN, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    ).derive(password.encode("utf-8"))
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check; ``False`` for anything malformed or out of bounds."""

    try:
        scheme, n_s, r_s, p_s, salt_s, dk_s = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        if not (2 <= n <= _MAX_N and n & (n - 1) == 0):
            return False
        if not (1 <= r <= _MAX_R and 1 <= p <= _MAX_P):
            return False
        salt, expected = _b64d(salt_s), _b64d(dk_s)
        if not (8 <= len(salt) <= 64 and 16 <= len(expected) <= 64):
            return False
        Scrypt(salt=salt, length=len(expected), n=n, r=r, p=p).verify(
            password.encode("utf-8"), expected
        )
    except (ValueError, TypeError, MemoryError, InvalidKey):
        return False
    return True


# A hash nobody knows the password to, verified when the username is unknown so
# an absent account costs the same time as a present one.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(24))


def validate_new_password(password: str, *, username: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise _coded("admin.auth.password_too_short", min=MIN_PASSWORD_LENGTH)
    if len(password) > MAX_PASSWORD_LENGTH:
        raise _coded("admin.auth.password_too_long", max=MAX_PASSWORD_LENGTH)
    if password.strip() != password:
        raise _coded("admin.auth.password_edge_spaces")
    if len(set(password)) < 5:
        raise _coded("admin.auth.password_repetitive")
    if username and username.lower() in password.lower():
        raise _coded("admin.auth.password_has_username")


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}[a-z0-9]$")


def normalise_username(raw: str) -> str:
    return (raw or "").strip().lower()


async def ensure_bootstrap_admin(
    session: AsyncSession, *, settings: Settings
) -> Optional[str]:
    """Create the first platform admin if there is none. Idempotent.

    Returns the generated password when one was generated (so a caller or test
    can see it), else ``None``. The generated password is also logged once.
    """

    existing = await session.scalar(select(func.count()).select_from(PlatformAdmin))
    if existing:
        await session.commit()
        return None

    username = normalise_username(settings.bootstrap_admin_username) or "admin"
    if not _USERNAME_RE.match(username):
        _log.error(
            "QUBERI_BOOTSTRAP_ADMIN_USERNAME %r is not a valid username "
            "(2-64 chars: letters, digits, . _ -); using 'admin'",
            username,
        )
        username = "admin"

    configured = settings.bootstrap_admin_password
    generated: Optional[str] = None
    if configured is not None and configured.get_secret_value():
        password = configured.get_secret_value()
        try:
            validate_new_password(password, username=username)
        except PlatformAdminError as exc:
            _log.error(
                "QUBERI_BOOTSTRAP_ADMIN_PASSWORD rejected (%s); "
                "generating a random one instead",
                exc.message,
            )
            password = generated = secrets.token_urlsafe(18)
    else:
        password = generated = secrets.token_urlsafe(18)

    admin = PlatformAdmin(
        username=username,
        password_hash=hash_password(password),
        must_change_password=generated is not None,
        token_version=1,
    )
    session.add(admin)
    try:
        await session.commit()
    except IntegrityError:
        # Another process created it first; nothing to do.
        await session.rollback()
        return None

    if generated is not None:
        _log.warning(
            "PLATFORM ADMIN CREATED. Sign in at /admin with username=%r "
            "password=%r . This is shown once; you must change it at first "
            "sign-in.",
            username,
            generated,
        )
    else:
        _log.warning("PLATFORM ADMIN CREATED with username=%r", username)
    return generated


# ---------------------------------------------------------------------------
# Sign-in with throttling
# ---------------------------------------------------------------------------

_WINDOW_SECONDS = 15 * 60
_MAX_FAILURES_PER_ACCOUNT = 5
_MAX_ATTEMPTS_PER_IP = 30
_PREFIX = "quberi:padmin:"


async def _redis_incr(redis: Redis, key: str) -> int:
    try:
        count = int(await redis.incr(key))
        if count == 1:
            await redis.expire(key, _WINDOW_SECONDS)
        return count
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise _coded("admin.auth.try_again_shortly", 503) from exc


async def _redis_count(redis: Redis, key: str) -> int:
    try:
        return int(await redis.get(key) or 0)
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise _coded("admin.auth.try_again_shortly", 503) from exc



async def authenticate(
    session: AsyncSession,
    redis: Redis,
    *,
    username: str,
    password: str,
    client_ip: str,
) -> PlatformAdmin:
    """Check a username and password; throttled, no account enumeration."""

    name = normalise_username(username)
    ip_key = f"{_PREFIX}ip:{client_ip}"
    acct_key = f"{_PREFIX}fail:{hashlib.sha256(name.encode()).hexdigest()}"

    if await _redis_incr(redis, ip_key) > _MAX_ATTEMPTS_PER_IP:
        raise _coded("admin.auth.too_many_attempts", 429)
    if await _redis_count(redis, acct_key) >= _MAX_FAILURES_PER_ACCOUNT:
        raise _coded("admin.auth.too_many_attempts", 429)

    admin = None
    if name and len(name) <= 64 and len(password) <= 1024:
        admin = await session.scalar(
            select(PlatformAdmin).where(PlatformAdmin.username == name)
        )
    stored = admin.password_hash if admin is not None else _DUMMY_HASH
    ok = verify_password(password if len(password) <= 1024 else "", stored)
    if admin is None or not ok:
        await session.commit()
        await _redis_incr(redis, acct_key)
        raise _coded("admin.auth.bad_credentials", 401)

    try:
        await redis.delete(acct_key)
    except Exception:  # noqa: BLE001 - a stale counter only costs the admin a wait
        _log.warning("could not clear sign-in failure counter", exc_info=True)
    admin.last_login_at = datetime.now(timezone.utc)
    await session.commit()
    return admin


async def change_password(
    session: AsyncSession,
    *,
    admin: PlatformAdmin,
    current_password: str,
    new_password: str,
) -> PlatformAdmin:
    if not verify_password(current_password, admin.password_hash):
        raise _coded("admin.auth.current_password_wrong", 403)
    validate_new_password(new_password, username=admin.username)
    if hmac.compare_digest(
        new_password.encode("utf-8"), current_password.encode("utf-8")
    ):
        raise _coded("admin.auth.password_unchanged")
    admin.password_hash = hash_password(new_password)
    admin.must_change_password = False
    admin.token_version = int(admin.token_version) + 1
    admin.password_changed_at = datetime.now(timezone.utc)
    await session.commit()
    return admin


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

_TOKEN_TYPE = "platform_admin"
_TOKEN_VERSION = 1


@dataclass(frozen=True)
class AdminSessionClaims:
    admin_id: uuid.UUID
    token_version: int


def issue_admin_token(admin: PlatformAdmin, *, ttl_seconds: int) -> tuple[str, int]:
    """Return ``(token, expires_at_epoch)``."""

    now = int(time.time())
    exp = now + int(ttl_seconds)
    sealed = crypto.seal(
        {
            "v": _TOKEN_VERSION,
            "typ": _TOKEN_TYPE,
            "aid": str(admin.id),
            "tv": int(admin.token_version),
            "iat": now,
            "exp": exp,
        }
    )
    assert sealed is not None  # noqa: S101
    return sealed, exp


def read_admin_token(token: str) -> Optional[AdminSessionClaims]:
    """Decode an admin session token; ``None`` unless it is one and unexpired."""

    if not token:
        return None
    try:
        payload = crypto.unseal(token)
    except crypto.CryptoError:
        return None
    if not isinstance(payload, dict) or payload.get("typ") != _TOKEN_TYPE:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or int(exp) < int(time.time()):
        return None
    try:
        return AdminSessionClaims(
            admin_id=uuid.UUID(str(payload["aid"])),
            token_version=int(payload["tv"]),
        )
    except (KeyError, ValueError, TypeError):
        return None


async def admin_for_token(
    session: AsyncSession, token: str
) -> Optional[PlatformAdmin]:
    """The account behind a valid, current admin session token, else ``None``."""

    claims = read_admin_token(token)
    if claims is None:
        return None
    admin = await session.get(PlatformAdmin, claims.admin_id)
    if admin is None or int(admin.token_version) != claims.token_version:
        return None
    return admin
