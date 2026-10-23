"""Platform admin sign-in for the ``/admin`` console.

POST /admin/auth/login            username + password -> admin session token
GET  /admin/auth/me               who am I (needs an admin session)
POST /admin/auth/change-password  current + new password -> fresh session token

The existing ``QUBERI_PLATFORM_ADMIN_TOKEN`` keeps working for scripts on the
other ``/admin/*`` endpoints; these three are about *accounts* and so need a
signed-in admin session, not the static token.

Throttling note: the per-address limit trusts ``CF-Connecting-IP`` /
``X-Forwarded-For`` (see ``client_ip``), which someone reaching the origin
directly can forge. The per-*account* lockout does not depend on it and is what
actually stops password guessing.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.admin_errors import AdminApiException
from quberi_api.api.dependencies import get_db_session, get_infrastructure
from quberi_api.api.security import require_platform_admin_session
from quberi_api.api.v1.guest_chat import client_ip
from quberi_api.core.config import Settings
from quberi_api.domain.platform_admin.models import PlatformAdmin
from quberi_api.infrastructure.registry import Infrastructure
from quberi_api.services import platform_admin as svc

admin_auth_router = APIRouter(prefix="/admin/auth", tags=["admin"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    # Not length-limited here beyond a sanity cap: rules are enforced where a
    # password is *chosen*, never where one is checked.
    password: str = Field(min_length=1, max_length=1024)


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


class AdminSessionResponse(BaseModel):
    token: str
    expires_at: int
    username: str
    must_change_password: bool


class AdminMeResponse(BaseModel):
    username: str
    must_change_password: bool


def _settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _raise(exc: svc.PlatformAdminError) -> None:
    if exc.code:
        raise AdminApiException(exc.status, exc.code, **exc.params) from exc
    raise HTTPException(status_code=exc.status, detail=exc.message) from exc


def _session_response(admin: PlatformAdmin, settings: Settings) -> AdminSessionResponse:
    token, exp = svc.issue_admin_token(
        admin, ttl_seconds=settings.platform_admin_session_ttl_seconds
    )
    return AdminSessionResponse(
        token=token,
        expires_at=exp,
        username=admin.username,
        must_change_password=bool(admin.must_change_password),
    )


@admin_auth_router.post(
    "/login",
    response_model=AdminSessionResponse,
    summary="Sign in as a platform admin",
)
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> AdminSessionResponse:
    try:
        admin = await svc.authenticate(
            session,
            infra.redis,
            username=payload.username,
            password=payload.password,
            client_ip=client_ip(request),
        )
    except svc.PlatformAdminError as exc:
        _raise(exc)
    return _session_response(admin, _settings(request))


@admin_auth_router.get(
    "/me",
    response_model=AdminMeResponse,
    summary="The signed-in platform admin",
)
async def me(
    admin: PlatformAdmin = Depends(require_platform_admin_session),
) -> AdminMeResponse:
    return AdminMeResponse(
        username=admin.username,
        must_change_password=bool(admin.must_change_password),
    )


@admin_auth_router.post(
    "/change-password",
    response_model=AdminSessionResponse,
    summary="Change the signed-in admin's password",
)
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    admin: PlatformAdmin = Depends(require_platform_admin_session),
    session: AsyncSession = Depends(get_db_session),
) -> AdminSessionResponse:
    try:
        updated = await svc.change_password(
            session,
            admin=admin,
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
    except svc.PlatformAdminError as exc:
        _raise(exc)
    # Older sessions died with the version bump; hand back a fresh one.
    return _session_response(updated, _settings(request))
