"""Authorization codes, tokens and the checks that guard them.

Flow (OAuth 2.1 authorization code + PKCE, public client):

1. The tool opens the browser at Quberi's consent page with a PKCE challenge.
2. The signed-in person picks an organisation and approves; we store a
   one-time *code* in Redis (60 s) bound to the challenge, the redirect
   address, the person and the organisation.
3. The tool exchanges code + PKCE verifier for an access token (1 h) and a
   refresh token (30 days, rotated on every use, 90 days at most overall).

Security properties kept here (each has a test):

* tokens are random, high-entropy, and stored only as SHA-256 hashes;
* a code works once (atomic ``GETDEL``) and only with the matching verifier,
  redirect address and client;
* a refresh token used twice revokes the whole grant (it was copied);
* every request re-checks that the person, the organisation and the
  organisation's "allow external tools" switch are still valid, so turning
  the switch off cuts access at once - it never waits for a token to expire.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from redis.asyncio import Redis
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.identity.models import Organization, User
from quberi_api.domain.oauth.models import OAuthGrant

CODE_PREFIX = "qbr_ac_"
ACCESS_PREFIX = "qbr_at_"
REFRESH_PREFIX = "qbr_rt_"

CODE_TTL_SECONDS = 60
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
ABSOLUTE_TTL = timedelta(days=90)
_LAST_USED_GRANULARITY = timedelta(seconds=60)
_CODE_KEY = "quberi:oauth:code:"

_VERIFIER_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9\-_]{43}$")


class OAuthError(Exception):
    """An OAuth-style failure with the standard ``error`` code."""

    def __init__(self, error: str, description: str, status: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status


class ExternalAuthError(Exception):
    """A bearer token was missing, wrong, expired, or no longer allowed."""

    def __init__(self, code: str, detail: str, status: int = 401) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


# Why a grant was revoked when a member is deactivated (shown in the audit log).
REVOKE_MEMBER_DEACTIVATED = "member_deactivated"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _audit(
    session: AsyncSession,
    grant: OAuthGrant,
    action: str,
    *,
    actor_type: str = "user",
    reason: Optional[str] = None,
    actor_user_id: Optional[uuid.UUID] = None,
) -> None:
    """Record a change to a grant. Ids and labels only - never a token.

    ``actor_user_id`` names someone other than the grant's holder who made the change
    (an administrator deactivating the member); without it the holder is the actor.
    """

    payload: dict = {
        "grant_id": str(grant.id),
        "client_id": grant.client_id,
        "client_name": grant.client_name,
    }
    if reason:
        payload["reason"] = reason
    session.add(
        AuditEvent(
            organization_id=grant.organization_id,
            actor_type=actor_type,
            actor_id=(actor_user_id or grant.user_id) if actor_type == "user" else None,
            action=action,
            resource_type="oauth_grant",
            resource_id=grant.id,
            payload=payload,
        )
    )


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def is_valid_challenge(challenge: str) -> bool:
    return bool(_CHALLENGE_RE.match(challenge or ""))


def verify_pkce(verifier: str, challenge: str) -> bool:
    """RFC 7636 ``S256``: base64url(sha256(verifier)) must equal the challenge."""

    if not _VERIFIER_RE.match(verifier or ""):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, challenge or "")


# ---------------------------------------------------------------------------
# Authorization codes (Redis, one use)
# ---------------------------------------------------------------------------


async def create_authorization_code(
    redis: Redis,
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    scope: str,
) -> str:
    code = _new_token(CODE_PREFIX)
    payload = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "challenge": code_challenge,
        "user_id": str(user_id),
        "organization_id": str(organization_id),
        "scope": scope,
    }
    await redis.set(
        _CODE_KEY + hash_token(code), json.dumps(payload), ex=CODE_TTL_SECONDS
    )
    return code


async def _consume_code(redis: Redis, code: str) -> Optional[dict]:
    if not code.startswith(CODE_PREFIX) or len(code) > 200:
        return None
    raw = await redis.getdel(_CODE_KEY + hash_token(code))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Token issue / exchange / refresh
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str
    grant_id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID
    client_id: str
    client_name: str

    def as_response(self) -> dict:
        return {
            "access_token": self.access_token,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
            "scope": self.scope,
        }


async def _check_principal(
    session: AsyncSession, *, user_id: uuid.UUID, organization_id: uuid.UUID
) -> tuple[User, Organization]:
    row = (
        await session.execute(
            select(User, Organization)
            .join(Organization, Organization.id == User.organization_id)
            .where(User.id == user_id, Organization.id == organization_id)
        )
    ).first()
    if row is None:
        raise OAuthError("invalid_grant", "the account is no longer available")
    user, org = row
    if user.status != "active" or org.status != "active":
        raise OAuthError("invalid_grant", "the account is no longer available")
    if not org.allow_external_tools:
        raise OAuthError(
            "access_denied",
            "this organization does not allow outside tools",
            status=403,
        )
    return user, org


async def exchange_code(
    session: AsyncSession,
    redis: Redis,
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str,
    client_name: str,
) -> IssuedTokens:
    """Trade a one-time code (+ PKCE verifier) for tokens."""

    data = await _consume_code(redis, code)
    # The code is already gone from Redis here, whatever happens next.
    if data is None:
        raise OAuthError("invalid_grant", "the code is invalid or has expired")
    if (
        data.get("client_id") != client_id
        or data.get("redirect_uri") != redirect_uri
        or not verify_pkce(code_verifier, str(data.get("challenge", "")))
    ):
        raise OAuthError("invalid_grant", "the code does not match this request")

    user_id = uuid.UUID(data["user_id"])
    organization_id = uuid.UUID(data["organization_id"])
    scope = str(data.get("scope", ""))

    async with session.begin():
        await _check_principal(
            session, user_id=user_id, organization_id=organization_id
        )
        now = _now()
        access = _new_token(ACCESS_PREFIX)
        refresh = _new_token(REFRESH_PREFIX)
        grant = OAuthGrant(
            organization_id=organization_id,
            user_id=user_id,
            client_id=client_id,
            client_name=client_name,
            scope=scope,
            access_hash=hash_token(access),
            access_expires_at=now + ACCESS_TTL,
            refresh_hash=hash_token(refresh),
            refresh_expires_at=now + REFRESH_TTL,
            absolute_expires_at=now + ABSOLUTE_TTL,
        )
        session.add(grant)
        await session.flush()
        grant_id = grant.id
        _audit(session, grant, "external_access.granted")
    return IssuedTokens(
        access_token=access,
        refresh_token=refresh,
        expires_in=int(ACCESS_TTL.total_seconds()),
        scope=scope,
        grant_id=grant_id,
        organization_id=organization_id,
        user_id=user_id,
        client_id=client_id,
        client_name=client_name,
    )


async def refresh_tokens(
    session: AsyncSession, *, refresh_token: str, client_id: str
) -> IssuedTokens:
    """Rotate: a new access and refresh token, the old refresh token dies."""

    if not refresh_token.startswith(REFRESH_PREFIX) or len(refresh_token) > 200:
        raise OAuthError("invalid_grant", "the refresh token is invalid")
    h = hash_token(refresh_token)

    result: Optional[IssuedTokens] = None
    error: Optional[OAuthError] = None

    async with session.begin():
        # Lock the row so two refreshes with the same token cannot both win.
        grant = (
            await session.execute(
                select(OAuthGrant).where(OAuthGrant.refresh_hash == h).with_for_update()
            )
        ).scalar_one_or_none()

        if grant is None:
            reused = (
                await session.execute(
                    select(OAuthGrant)
                    .where(OAuthGrant.previous_refresh_hash == h)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if reused is not None and reused.revoked_at is None:
                # An old refresh token showed up again: someone kept a copy.
                reused.revoked_at = _now()
                reused.revoke_reason = "refresh_reuse"
                _audit(
                    session,
                    reused,
                    "external_access.revoked",
                    actor_type="system",
                    reason="refresh_reuse",
                )
            error = OAuthError("invalid_grant", "the refresh token is invalid")
        else:
            now = _now()
            if grant.client_id != client_id:
                error = OAuthError("invalid_grant", "the refresh token is invalid")
            elif grant.revoked_at is not None:
                error = OAuthError("invalid_grant", "the grant was revoked")
            elif grant.refresh_expires_at <= now or grant.absolute_expires_at <= now:
                error = OAuthError("invalid_grant", "the grant has expired")
            else:
                try:
                    await _check_principal(
                        session,
                        user_id=grant.user_id,
                        organization_id=grant.organization_id,
                    )
                except OAuthError as exc:
                    error = exc
            if error is None:
                assert grant is not None  # noqa: S101
                access = _new_token(ACCESS_PREFIX)
                refresh = _new_token(REFRESH_PREFIX)
                grant.previous_refresh_hash = grant.refresh_hash
                grant.refresh_hash = hash_token(refresh)
                grant.access_hash = hash_token(access)
                grant.access_expires_at = now + ACCESS_TTL
                grant.refresh_expires_at = min(
                    now + REFRESH_TTL, grant.absolute_expires_at
                )
                result = IssuedTokens(
                    access_token=access,
                    refresh_token=refresh,
                    expires_in=int(ACCESS_TTL.total_seconds()),
                    scope=grant.scope,
                    grant_id=grant.id,
                    organization_id=grant.organization_id,
                    user_id=grant.user_id,
                    client_id=grant.client_id,
                    client_name=grant.client_name,
                )

    # The revocation above must be committed even though we then raise.
    if error is not None:
        raise error
    assert result is not None  # noqa: S101
    return result


async def revoke_by_token(session: AsyncSession, token: str) -> Optional[OAuthGrant]:
    """RFC 7009: revoke the grant behind an access or refresh token, if any."""

    if len(token) > 200:
        return None
    h = hash_token(token)
    async with session.begin():
        grant = (
            await session.execute(
                select(OAuthGrant).where(
                    (OAuthGrant.access_hash == h) | (OAuthGrant.refresh_hash == h)
                )
            )
        ).scalar_one_or_none()
        if grant is not None and grant.revoked_at is None:
            grant.revoked_at = _now()
            grant.revoke_reason = "revoked_by_client"
            _audit(
                session, grant, "external_access.revoked", reason="revoked_by_client"
            )
        return grant


async def revoke_grant_for_user(
    session: AsyncSession, *, grant_id: uuid.UUID, identity_id: uuid.UUID
) -> bool:
    """A person revokes one of their own grants (any organisation they belong to)."""

    async with session.begin():
        grant = (
            await session.execute(
                select(OAuthGrant)
                .join(User, User.id == OAuthGrant.user_id)
                .where(OAuthGrant.id == grant_id, User.identity_id == identity_id)
            )
        ).scalar_one_or_none()
        if grant is None:
            return False
        if grant.revoked_at is None:
            grant.revoked_at = _now()
            grant.revoke_reason = "revoked_by_user"
            _audit(
                session, grant, "external_access.revoked", reason="revoked_by_user"
            )
        return True


async def revoke_all_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    reason: str,
) -> int:
    """Revoke every live grant of one member; returns how many were revoked.

    Runs inside the caller's transaction (it opens none), so deactivating a member
    ends their outside-tool access in the same commit as everything else.
    Grants that are already revoked are left as they are, so a second call changes
    nothing and writes nothing.
    """

    grants = (
        (
            await session.execute(
                select(OAuthGrant)
                .where(OAuthGrant.user_id == user_id, OAuthGrant.revoked_at.is_(None))
                .order_by(OAuthGrant.created_at, OAuthGrant.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    now = _now()
    for grant in grants:
        grant.revoked_at = now
        grant.revoke_reason = reason
        _audit(
            session,
            grant,
            "external_access.revoked",
            reason=reason,
            actor_user_id=actor_user_id,
        )
    return len(grants)


# ---------------------------------------------------------------------------
# Using an access token
# ---------------------------------------------------------------------------


async def record_caller(
    session: AsyncSession, *, grant_id: uuid.UUID, label: str
) -> None:
    """Remember which program is behind a grant. Writes only when the label changes."""

    await session.execute(
        update(OAuthGrant)
        .where(
            OAuthGrant.id == grant_id,
            OAuthGrant.revoked_at.is_(None),
            or_(OAuthGrant.caller_label.is_(None), OAuthGrant.caller_label != label),
        )
        .values(caller_label=label)
    )
    await session.commit()


@dataclass(frozen=True)
class ExternalCaller:
    grant_id: uuid.UUID
    client_id: str
    client_name: str
    scope: str
    user: User
    organization: Organization

    def has_scope(self, scope: str) -> bool:
        return scope in self.scope.split()


async def authenticate_access_token(
    session: AsyncSession, token: str
) -> ExternalCaller:
    """Resolve a bearer access token to the person it acts for."""

    if not token.startswith(ACCESS_PREFIX) or len(token) > 200:
        raise ExternalAuthError("invalid_token", "invalid access token")
    h = hash_token(token)
    async with session.begin():
        row = (
            await session.execute(
                select(OAuthGrant, User, Organization)
                .join(User, User.id == OAuthGrant.user_id)
                .join(Organization, Organization.id == OAuthGrant.organization_id)
                .where(OAuthGrant.access_hash == h)
            )
        ).first()
        if row is None:
            raise ExternalAuthError("invalid_token", "invalid access token")
        grant, user, org = row
        now = _now()
        if (
            grant.revoked_at is not None
            or grant.access_expires_at <= now
            or grant.absolute_expires_at <= now
            or user.status != "active"
            or org.status != "active"
        ):
            raise ExternalAuthError("invalid_token", "invalid access token")
        if not org.allow_external_tools:
            raise ExternalAuthError(
                "external_tools_disabled",
                "this organization does not allow outside tools",
                status=403,
            )
        if (
            grant.last_used_at is None
            or now - grant.last_used_at >= _LAST_USED_GRANULARITY
        ):
            grant.last_used_at = now
        return ExternalCaller(
            grant_id=grant.id,
            client_id=grant.client_id,
            client_name=grant.client_name,
            scope=grant.scope,
            user=user,
            organization=org,
        )


# ---------------------------------------------------------------------------
# Rate limiting (Redis, fails closed)
# ---------------------------------------------------------------------------


async def rate_limit(
    redis: Redis, *, key: str, limit: int, window_seconds: int
) -> None:
    """Allow ``limit`` calls per ``window_seconds``; otherwise 429.

    If Redis cannot be reached the call is refused: failing open would turn an
    outage into an unlimited guessing window on the token endpoint.
    """

    try:
        count = int(await redis.incr(key))
        if count == 1:
            await redis.expire(key, window_seconds)
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise OAuthError(
            "temporarily_unavailable", "try again shortly", status=503
        ) from exc
    if count > limit:
        raise OAuthError("rate_limited", "too many requests", status=429)
