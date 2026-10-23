"""API security dependencies.

Two dependencies live here today:

* ``require_platform_admin`` — bearer-token gate for ``/api/v1/admin/*``.
* ``require_caller`` — resolves the calling *user* for the tenant-scoped API
  surface. Until real SSO lands, callers authenticate by presenting the
  platform admin bearer token plus two headers that name the user:

      X-Quberi-Organization: <organization-slug>
      X-Quberi-User-Email:   <user-email>

  The token proves "operator-level access to this deployment"; the headers
  say "act as this user inside this tenant". Real per-user identity replaces
  this whole scheme without changing the tenant-scoped endpoints themselves —
  they only depend on the resolved ``CallerContext``.

The bearer compare uses ``secrets.compare_digest`` to keep the shared secret
from leaking one byte at a time.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.admin_errors import AdminApiException
from quberi_api.api.dependencies import get_db_session
from quberi_api.core.config import Settings
from quberi_api.domain.auth.models import RevokedSession
from quberi_api.domain.enums import BLOCKED_USER_STATUSES
from quberi_api.domain.identity.models import Organization, User
from quberi_api.domain.platform_admin.models import PlatformAdmin
from quberi_api.services import platform_admin as platform_admin_service
from quberi_api.services.auth.session import (
    SessionClaims,
    read_session,
    session_token_hash,
)

log = logging.getLogger(__name__)

# Bearer scheme. ``auto_error=False`` lets us produce our own 401 wording
# rather than FastAPI's default ``{"detail": "Not authenticated"}``.
_bearer_scheme = HTTPBearer(auto_error=False, bearerFormat="opaque")


# ---------------------------------------------------------------------------
# Settings accessor
# ---------------------------------------------------------------------------


def _get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


# ---------------------------------------------------------------------------
# Platform admin gate
# ---------------------------------------------------------------------------


def _validate_admin_token(
    credentials: Optional[HTTPAuthorizationCredentials],
    settings: Settings,
) -> None:
    """Shared bearer-token check used by both dependencies below.

    Raises the same 401 responses so callers cannot fingerprint which
    dependency is guarding a route.
    """

    configured = settings.platform_admin_token
    if configured is None or not configured.get_secret_value():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="platform admin API is disabled on this deployment",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not secrets.compare_digest(
        credentials.credentials, configured.get_secret_value()
    ):
        raise AdminApiException(
            status.HTTP_401_UNAUTHORIZED,
            "admin.auth.invalid_token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _matches_static_token(
    credentials: Optional[HTTPAuthorizationCredentials], settings: Settings
) -> bool:
    configured = settings.platform_admin_token
    if configured is None or not configured.get_secret_value():
        return False
    if credentials is None or credentials.scheme.lower() != "bearer":
        return False
    return secrets.compare_digest(
        credentials.credentials, configured.get_secret_value()
    )


async def _admin_from_session_token(
    credentials: Optional[HTTPAuthorizationCredentials],
    session: AsyncSession,
) -> Optional[PlatformAdmin]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        return None
    admin = await platform_admin_service.admin_for_token(
        session, credentials.credentials
    )
    # The lookup auto-began a read transaction; close it so the handler can
    # open its own write transaction.
    await session.commit()
    return admin


async def require_platform_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    settings: Settings = Depends(_get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    """Let the request through with the configured admin token (scripts, curl)
    or with a signed-in platform admin's session (the ``/admin`` console).

    An account that still has to change its first-boot password does not get
    through here: it can only reach the password-change endpoint.
    """

    if _matches_static_token(credentials, settings):
        return
    admin = await _admin_from_session_token(credentials, session)
    if admin is not None and not admin.must_change_password:
        return
    # Neither matched. ``_validate_admin_token`` always raises here (the token
    # is unset, absent, or wrong), keeping the exact 401 wording callers saw
    # before accounts existed.
    _validate_admin_token(credentials, settings)
    # Reached only if a static token is configured and matched, which the first
    # check already handled; keep the guard so this can never fall through open.
    raise AdminApiException(
        status.HTTP_401_UNAUTHORIZED,
        "admin.auth.invalid_token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_platform_admin_session(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> PlatformAdmin:
    """A signed-in platform admin *account* (not the static token), including
    one that must still change its password. Used by the account endpoints."""

    admin = await _admin_from_session_token(credentials, session)
    if admin is None:
        raise AdminApiException(
            status.HTTP_401_UNAUTHORIZED,
            "admin.auth.sign_in_required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return admin


# ---------------------------------------------------------------------------
# Caller resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallerContext:
    """Resolved identity of whoever is calling a tenant-scoped endpoint."""

    user: User
    organization: Organization
    is_platform_admin: bool
    # True for anonymous guest sessions (bound to the demo org, read-only).
    is_guest: bool = False


def _read_header(request: Request, name: str) -> str:
    value = request.headers.get(name)
    if value is None or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"missing header: {name}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return value.strip()


def _bearer_token(
    credentials: Optional[HTTPAuthorizationCredentials],
) -> Optional[str]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        return None
    return credentials.credentials


async def optional_bearer_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> Optional[str]:
    """The bearer token if one was sent, else ``None`` (never raises)."""

    return _bearer_token(credentials)


class SessionInvalidError(HTTPException):
    """The session can no longer be used: the account is unavailable, the token was
    signed out, or the member's sessions were revoked.

    It is an ``HTTPException`` so an app without the custom handler still answers a
    plain 401. The handler in ``api.errors`` adds ``code: "session_invalid"``, which is
    the only thing the web client treats as "sign out now".
    """

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="session no longer valid",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _rejection_reason(row, claims: SessionClaims) -> Optional[str]:
    """Why a session token must be refused, or ``None`` to let it through.

    Logged for operators; the caller always gets the same 401, so nobody can tell
    "deactivated" from "expired".
    """

    if row is None:
        return "unknown_user_or_organization"
    user, organization, revoked = row
    if user.status in BLOCKED_USER_STATUSES:
        return "user_not_active"
    if organization.status != "active":
        return "organization_not_active"
    if claims.session_version != user.session_version:
        return "session_version_changed"
    if revoked is not None:
        return "signed_out"
    return None


async def require_caller(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    settings: Settings = Depends(_get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> CallerContext:
    """Resolve the calling user (tenant-scoped endpoints).

    Two paths:

    1. **Real session** — a ``Bearer`` session token minted by
       ``/auth/verify``. Decoded + validated, then the user + org are
       loaded by id. This is the only accepted path in production.
    2. **Dev impersonation** (non-production only) — the platform admin
       token plus ``X-Quberi-Organization`` / ``X-Quberi-User-Email``
       headers, kept so local development can switch users freely.
    """

    token = _bearer_token(credentials)

    # ---- Path 1: real session token ----
    if token:
        claims = read_session(token)
        if claims is not None:
            # One round trip: user + organization, plus a primary-key look-up of
            # the signed-out list (usually no row).
            stmt = (
                select(User, Organization, RevokedSession.token_hash)
                .join(Organization, Organization.id == User.organization_id)
                .outerjoin(
                    RevokedSession,
                    RevokedSession.token_hash == session_token_hash(token),
                )
                .where(User.id == claims.user_id)
                .where(Organization.id == claims.organization_id)
            )
            row = (await session.execute(stmt)).first()
            reason = _rejection_reason(row, claims)
            if reason is not None:
                await session.commit()
                log.info("session rejected: %s user=%s", reason, claims.user_id)
                raise SessionInvalidError()
            user, organization, _ = row
            if user.status == "invited" and not claims.is_guest:
                # A session token is only minted after a successful sign-in, so someone
                # holding one has accepted their invitation. Mark them active, once.
                await session.execute(
                    update(User)
                    .where(User.id == user.id, User.status == "invited")
                    .values(status="active")
                )
                await session.commit()
                await session.refresh(user)
            else:
                await session.commit()
            return CallerContext(
                user=user,
                organization=organization,
                is_platform_admin=False,
                is_guest=claims.is_guest,
            )

    # ---- Path 2: dev impersonation (non-production only) ----
    if settings.environment == "production":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="a valid session is required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    _validate_admin_token(credentials, settings)

    org_slug = _read_header(request, "X-Quberi-Organization").lower()
    user_email = _read_header(request, "X-Quberi-User-Email").lower()

    stmt = (
        select(User, Organization)
        .join(Organization, Organization.id == User.organization_id)
        .where(Organization.slug == org_slug)
        .where(User.email == user_email)
        .where(User.status.notin_(BLOCKED_USER_STATUSES))
        .where(Organization.status == "active")
    )
    row = (await session.execute(stmt)).first()
    await session.commit()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="caller not found for the given organization and email",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user, organization = row
    return CallerContext(
        user=user,
        organization=organization,
        is_platform_admin=True,
    )
