"""Personal account: profile + linked login methods (email, Feishu)."""

from __future__ import annotations

import io
import time
import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session, get_infrastructure
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.core.config import get_settings
from quberi_api.infrastructure.registry import Infrastructure
from quberi_api.services import account as account_service
from quberi_api.services.auth import feishu_login as feishu_login_service
from quberi_api.services.auth import google_login as google_login_service
from quberi_api.services.auth import verification as verification_service

router = APIRouter(prefix="/account", tags=["account"])

_GENERIC_SENT = "If that email is available, a code is on its way."


class AccountResponse(BaseModel):
    display_name: str
    avatar_url: str | None = None
    email: str
    email_is_placeholder: bool
    email_verified: bool
    has_email_login: bool
    phone: str | None = None
    has_phone: bool = False
    feishu_linked: bool
    feishu_account: str | None = None
    google_linked: bool = False
    google_account: str | None = None


def _to_response(view: account_service.AccountView) -> AccountResponse:
    return AccountResponse(
        display_name=view.display_name,
        avatar_url=view.avatar_url,
        email=view.email,
        email_is_placeholder=view.email_is_placeholder,
        email_verified=view.email_verified,
        has_email_login=view.has_email_login,
        phone=view.phone,
        has_phone=view.has_phone,
        feishu_linked=view.feishu_linked,
        feishu_account=view.feishu_account,
        google_linked=view.google_linked,
        google_account=view.google_account,
    )


class BindEmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: EmailStr


class VerifyEmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: EmailStr
    code: str = Field(min_length=4, max_length=12)


class BindPhoneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    phone: str = Field(min_length=5, max_length=24)


class VerifyPhoneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    phone: str = Field(min_length=5, max_length=24)
    code: str = Field(min_length=4, max_length=12)


class GenericOk(BaseModel):
    ok: bool = True
    message: str = ""


class FeishuLinkUrlResponse(BaseModel):
    authorize_url: str


class GoogleLinkUrlResponse(BaseModel):
    authorize_url: str


@router.get("", response_model=AccountResponse, summary="Current account profile")
async def get_account(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AccountResponse:
    view = await account_service.get_account(
        session, user_id=caller.user.id, identity_id=caller.user.identity_id
    )
    return _to_response(view)


class SetNameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str = Field(min_length=1, max_length=120)


@router.post(
    "/name",
    response_model=AccountResponse,
    summary="Update the account display name",
)
async def set_name(
    payload: SetNameRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AccountResponse:
    await account_service.set_display_name(
        session,
        user_id=caller.user.id,
        organization_id=caller.organization.id,
        display_name=payload.display_name,
    )
    view = await account_service.get_account(
        session, user_id=caller.user.id, identity_id=caller.user.identity_id
    )
    return _to_response(view)


@router.post(
    "/email/request-code",
    response_model=GenericOk,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send a code to bind/verify a real email",
)
async def request_email_code(
    payload: BindEmailRequest,
    request: Request,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> GenericOk:
    ip = request.client.host if request.client else None
    try:
        await account_service.request_email_bind(
            session,
            identity_id=caller.user.identity_id,
            new_email=str(payload.email),
            ip_address=ip,
        )
    except account_service.AlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return GenericOk(ok=True, message=_GENERIC_SENT)


@router.post(
    "/email/verify",
    response_model=AccountResponse,
    summary="Verify the code and set the account email",
)
async def verify_email(
    payload: VerifyEmailRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AccountResponse:
    try:
        await account_service.confirm_email_bind(
            session,
            identity_id=caller.user.identity_id,
            user_id=caller.user.id,
            organization_id=caller.organization.id,
            new_email=str(payload.email),
            code=payload.code,
        )
    except verification_service.VerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    except account_service.AlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    # Return the refreshed profile.
    view = await account_service.get_account(
        session, user_id=caller.user.id, identity_id=caller.user.identity_id
    )
    return _to_response(view)


@router.post(
    "/phone/request-code",
    response_model=GenericOk,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send an SMS code to bind/verify a phone number",
)
async def request_phone_code(
    payload: BindPhoneRequest,
    request: Request,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> GenericOk:
    ip = request.client.host if request.client else None
    try:
        await account_service.request_phone_bind(
            session,
            identity_id=caller.user.identity_id,
            new_phone=payload.phone,
            ip_address=ip,
        )
    except account_service.AlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return GenericOk(ok=True, message="Verification code sent.")


@router.post(
    "/phone/verify",
    response_model=AccountResponse,
    summary="Verify the code and set the account phone",
)
async def verify_phone(
    payload: VerifyPhoneRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AccountResponse:
    try:
        await account_service.confirm_phone_bind(
            session,
            identity_id=caller.user.identity_id,
            new_phone=payload.phone,
            code=payload.code,
        )
    except verification_service.VerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    except account_service.AlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    view = await account_service.get_account(
        session, user_id=caller.user.id, identity_id=caller.user.identity_id
    )
    return _to_response(view)


@router.post(
    "/feishu/authorize-url",
    response_model=FeishuLinkUrlResponse,
    summary="Mint a Feishu consent URL to link Feishu to this account",
)
async def feishu_link_url(
    caller: CallerContext = Depends(require_caller),
) -> FeishuLinkUrlResponse:
    base = get_settings().public_base_url
    if not base:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="QUBERI_PUBLIC_BASE_URL is not configured.",
        )
    redirect_uri = f"{base.rstrip('/')}/api/v1/auth/feishu/callback"
    try:
        url = feishu_login_service.build_link_authorize_url(
            redirect_uri=redirect_uri,
            identity_id=str(caller.user.identity_id),
        )
    except feishu_login_service.FeishuLoginError as exc:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED, detail=str(exc)
        ) from exc
    return FeishuLinkUrlResponse(authorize_url=url)


@router.post(
    "/google/authorize-url",
    response_model=GoogleLinkUrlResponse,
    summary="Mint a Google consent URL to link Google to this account",
)
async def google_link_url(
    caller: CallerContext = Depends(require_caller),
) -> GoogleLinkUrlResponse:
    base = get_settings().public_base_url
    if not base:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="QUBERI_PUBLIC_BASE_URL is not configured.",
        )
    redirect_uri = f"{base.rstrip('/')}/api/v1/auth/google/callback"
    try:
        url = google_login_service.build_link_authorize_url(
            redirect_uri=redirect_uri,
            identity_id=str(caller.user.identity_id),
        )
    except google_login_service.GoogleLoginError as exc:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED, detail=str(exc)
        ) from exc
    return GoogleLinkUrlResponse(authorize_url=url)


@router.post(
    "/google/unlink",
    response_model=AccountResponse,
    summary="Remove the linked Google account",
)
async def google_unlink(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AccountResponse:
    try:
        await account_service.unlink_google(
            session, identity_id=caller.user.identity_id
        )
    except account_service.InvalidInputError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    view = await account_service.get_account(
        session, user_id=caller.user.id, identity_id=caller.user.identity_id
    )
    return _to_response(view)


# ---------------------------------------------------------------------------
# Avatar upload + public serve
# ---------------------------------------------------------------------------

_AVATAR_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_AVATAR_MAX_BYTES = 2 * 1024 * 1024  # 2 MB


def _avatar_key(user_id: uuid.UUID) -> str:
    return f"avatars/{user_id}"


@router.post(
    "/avatar",
    response_model=AccountResponse,
    summary="Upload/replace the account avatar image",
)
async def upload_avatar(
    file: UploadFile = File(...),
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> AccountResponse:
    content_type = (file.content_type or "").lower()
    if content_type not in _AVATAR_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="avatar must be a PNG, JPEG, WebP or GIF image",
        )
    data = await file.read(_AVATAR_MAX_BYTES + 1)
    if len(data) > _AVATAR_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="avatar must be 2 MB or smaller",
        )

    settings = get_settings()
    bucket = settings.minio_bucket
    client = infra.object_storage
    # Ensure the bucket exists (documents use the same one; guard anyway).
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
    client.put_object(
        bucket,
        _avatar_key(caller.user.id),
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )

    url = f"/api/v1/account/avatar/{caller.user.id}?v={int(time.time())}"
    await account_service.set_avatar_url(
        session,
        user_id=caller.user.id,
        organization_id=caller.organization.id,
        avatar_url=url,
    )
    view = await account_service.get_account(
        session, user_id=caller.user.id, identity_id=caller.user.identity_id
    )
    return _to_response(view)


@router.get(
    "/avatar/{user_id}",
    summary="Serve an account avatar image (public)",
)
async def get_avatar(
    user_id: uuid.UUID,
    infra: Infrastructure = Depends(get_infrastructure),
) -> Response:
    settings = get_settings()
    try:
        resp = infra.object_storage.get_object(settings.minio_bucket, _avatar_key(user_id))
        data = resp.read()
        ctype = resp.headers.get("Content-Type", "image/png")
        resp.close()
        resp.release_conn()
    except Exception:  # noqa: BLE001 — missing object → 404
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no avatar")
    return Response(
        content=data,
        media_type=ctype,
        headers={"Cache-Control": "public, max-age=3600"},
    )


account_router = router
