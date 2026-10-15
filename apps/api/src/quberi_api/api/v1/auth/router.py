"""Authentication endpoints.

Public (no session required):
* POST /api/v1/auth/request-code  — email a one-time code (invite-first)
* POST /api/v1/auth/verify        — exchange code for a session token

Session-scoped:
* GET  /api/v1/auth/me            — current user/org
* POST /api/v1/auth/logout        — client-side token drop (stateless)

Platform-admin (bearer admin token):
* GET  /api/v1/admin/email-settings
* PUT  /api/v1/admin/email-settings
* POST /api/v1/admin/email-settings/test
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.core.config import get_settings
from quberi_api.domain.identity.models import Organization, User
from quberi_api.api.security import (
    CallerContext,
    require_caller,
    require_platform_admin,
)
from quberi_api.api.v1.auth.schemas import (
    EmailProvider,
    EmailProvidersResponse,
    FeishuBindPhoneRequestCode,
    FeishuBindPhoneVerify,
    FeishuBindRequestCode,
    FeishuBindVerify,
    MeResponse,
    PhoneRequestCodeRequest,
    PhoneVerifyRequest,
    RequestCodeRequest,
    RequestCodeResponse,
    SessionUser,
    TestEmailRequest,
    VerifyCodeRequest,
    VerifyCodeResponse,
)
from quberi_api.services import authz as authz_service
from quberi_api.services import demo_seed
from quberi_api.services import org_membership as org_membership_service
from quberi_api.services.auth import email_settings as email_settings_service
from quberi_api.services.auth import feishu_login as feishu_login_service
from quberi_api.services.auth import google_login as google_login_service
from quberi_api.services.auth import phone_auth as phone_auth_service
from quberi_api.services.auth import verification as verification_service
from quberi_api.services.auth.email_sender import (
    EmailSendError,
    send_email_with_failover,
)
from quberi_api.services.auth.session import issue_session

auth_router = APIRouter(prefix="/auth", tags=["auth"])
email_settings_router = APIRouter(prefix="/admin", tags=["auth"])

# Generic response so the endpoint never reveals whether an email exists.
_GENERIC_SENT = "If that email belongs to an account, a code is on its way."
# Phone flow auto-registers, so we can be direct here.
_PHONE_SENT = "Verification code sent."


@auth_router.post(
    "/request-code",
    response_model=RequestCodeResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request a login verification code by email",
)
async def request_code(
    payload: RequestCodeRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> RequestCodeResponse:
    ip = request.client.host if request.client else None
    await verification_service.request_code(
        session, email=str(payload.email), purpose="login", ip_address=ip
    )
    return RequestCodeResponse(ok=True, message=_GENERIC_SENT)


@auth_router.post(
    "/verify",
    response_model=VerifyCodeResponse,
    summary="Verify a code and receive a session token",
)
async def verify(
    payload: VerifyCodeRequest,
    session: AsyncSession = Depends(get_db_session),
) -> VerifyCodeResponse:
    try:
        resolved = await verification_service.verify_code(
            session,
            email=str(payload.email),
            code=payload.code,
            purpose="login",
            display_name=payload.display_name,
        )
    except verification_service.VerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    token = issue_session(
        identity_id=uuid.UUID(resolved.identity_id),
        user_id=uuid.UUID(resolved.user_id),
        organization_id=uuid.UUID(resolved.organization_id),
        email=resolved.email,
    )
    org_role = await authz_service.get_direct_org_role(
        session,
        organization_id=uuid.UUID(resolved.organization_id),
        user_id=uuid.UUID(resolved.user_id),
    )
    return VerifyCodeResponse(
        token=token,
        user=SessionUser(
            user_id=uuid.UUID(resolved.user_id),
            email=resolved.email,
            display_name=resolved.display_name,
            organization_id=uuid.UUID(resolved.organization_id),
            organization_slug=resolved.organization_slug,
            organization_name=resolved.organization_name,
            organization_tier=resolved.organization_tier,
            org_role=org_role,
        ),
    )


@auth_router.post(
    "/guest",
    response_model=VerifyCodeResponse,
    summary="Start an anonymous guest session (demo org, read-only)",
)
async def guest(
    session: AsyncSession = Depends(get_db_session),
) -> VerifyCodeResponse:
    # Ensure the demo org exists (idempotent), then mint a read-only guest
    # session bound to its synthetic guest user.
    refs = await demo_seed.get_demo_refs(session)
    if refs is None:
        refs = await demo_seed.ensure_demo_org(session)

    org = (
        await session.execute(
            select(Organization).where(Organization.id == refs.organization_id)
        )
    ).scalar_one()
    guest_user = (
        await session.execute(
            select(User).where(User.id == refs.guest_user_id)
        )
    ).scalar_one()

    import uuid

    token = issue_session(
        identity_id=uuid.UUID(str(refs.guest_identity_id)),
        user_id=uuid.UUID(str(refs.guest_user_id)),
        organization_id=uuid.UUID(str(refs.organization_id)),
        email=guest_user.email,
        is_guest=True,
    )
    return VerifyCodeResponse(
        token=token,
        user=SessionUser(
            user_id=guest_user.id,
            email=guest_user.email,
            display_name=guest_user.display_name,
            organization_id=org.id,
            organization_slug=org.slug,
            organization_name=org.name,
            organization_tier=org.tier,
            is_guest=True,
        ),
    )


# ---------------------------------------------------------------------------
# Phone verification-code login (login-or-signup)
# ---------------------------------------------------------------------------


@auth_router.post(
    "/phone/request-code",
    response_model=RequestCodeResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request an SMS verification code",
)
async def phone_request_code(
    payload: PhoneRequestCodeRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> RequestCodeResponse:
    ip = request.client.host if request.client else None
    await phone_auth_service.request_phone_code(
        session, phone=payload.phone, ip_address=ip
    )
    return RequestCodeResponse(ok=True, message=_PHONE_SENT)


@auth_router.post(
    "/phone/verify",
    response_model=VerifyCodeResponse,
    summary="Verify an SMS code and sign in (auto-registers a personal account)",
)
async def phone_verify(
    payload: PhoneVerifyRequest,
    session: AsyncSession = Depends(get_db_session),
) -> VerifyCodeResponse:
    try:
        resolved = await phone_auth_service.verify_phone_and_resolve(
            session,
            phone=payload.phone,
            code=payload.code,
            workspace_name=payload.workspace_name,
            display_name=payload.display_name,
        )
    except verification_service.VerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    except phone_auth_service.PhoneSignupError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    import uuid

    token = issue_session(
        identity_id=uuid.UUID(resolved.identity_id),
        user_id=uuid.UUID(resolved.user_id),
        organization_id=uuid.UUID(resolved.organization_id),
        email=resolved.email,
    )
    org_role = await authz_service.get_direct_org_role(
        session,
        organization_id=uuid.UUID(resolved.organization_id),
        user_id=uuid.UUID(resolved.user_id),
    )
    return VerifyCodeResponse(
        token=token,
        user=SessionUser(
            user_id=uuid.UUID(resolved.user_id),
            email=resolved.email,
            display_name=resolved.display_name,
            organization_id=uuid.UUID(resolved.organization_id),
            organization_slug=resolved.organization_slug,
            organization_name=resolved.organization_name,
            organization_tier=resolved.organization_tier,
            org_role=org_role,
        ),
    )


# ---------------------------------------------------------------------------
# Feishu (Lark) SSO login
# ---------------------------------------------------------------------------


def _feishu_login_redirect_uri() -> str:
    base = get_settings().public_base_url
    if not base:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="QUBERI_PUBLIC_BASE_URL is not configured; Feishu login "
            "needs a public callback URL.",
        )
    return f"{base.rstrip('/')}/api/v1/auth/feishu/callback"


class FeishuAuthorizeUrlResponse(BaseModel):
    authorize_url: str


@auth_router.post(
    "/feishu/authorize-url",
    response_model=FeishuAuthorizeUrlResponse,
    summary="Mint a Feishu SSO consent URL (login/signup)",
)
async def feishu_authorize_url() -> FeishuAuthorizeUrlResponse:
    try:
        url = feishu_login_service.build_login_authorize_url(
            redirect_uri=_feishu_login_redirect_uri()
        )
    except feishu_login_service.FeishuLoginError as exc:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED, detail=str(exc)
        ) from exc
    return FeishuAuthorizeUrlResponse(authorize_url=url)


def _oauth_popup_html(*, source: str, ok: bool, payload: dict) -> str:
    """A small, branded status page for an OAuth popup window (Feishu,
    Google, ...). ``source`` is the ``postMessage`` discriminator the
    opener listens for (e.g. ``"quberi-feishu-login"``).

    It posts the result back to the opener and (on success/pending)
    auto-closes. The payload JSON is injected as inert text and read via
    ``JSON.parse`` — never interpolated into executable JS — and ``<`` is
    escaped so a display name can't break out of the script tag.
    """

    import html as _html
    import json as _json

    data = _json.dumps({"source": source, "ok": ok, **payload})
    data = data.replace("<", "\\u003c")

    pending = bool(payload.get("pending"))
    if not ok:
        state = "error"
        title = "登录失败"
        subtitle = _html.escape(str(payload.get("message") or "请重试 · Please try again"))
        icon_html = '<span class="q-glyph">!</span>'
        auto_close_ms = 0
    elif pending:
        state = "pending"
        title = "请在原窗口继续"
        subtitle = "Continue in the original window"
        icon_html = '<span class="q-spin" aria-hidden="true"></span>'
        auto_close_ms = 1400
    else:
        state = "success"
        title = "登录成功"
        subtitle = "You can close this window"
        icon_html = (
            '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" '
            'stroke="#fff" stroke-width="3" stroke-linecap="round" '
            'stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>'
        )
        auto_close_ms = 1400

    close_js = (
        f"setTimeout(function(){{window.close();}},{auto_close_ms});"
        if auto_close_ms
        else ""
    )

    template = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quberi</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
      "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    display: flex; align-items: center; justify-content: center;
    background:
      radial-gradient(60% 60% at 15% 10%, rgba(141, 92, 240, .18), transparent 60%),
      radial-gradient(55% 55% at 90% 90%, rgba(109, 59, 212, .16), transparent 60%),
      #f6f4fb;
    color: #211a33;
  }
  .q-card {
    width: 320px; max-width: calc(100vw - 40px);
    background: #fff; border-radius: 22px; padding: 34px 30px 30px;
    text-align: center;
    box-shadow: 0 24px 60px rgba(78, 47, 168, .18), 0 2px 8px rgba(78, 47, 168, .08);
    animation: q-in .28s cubic-bezier(.2,.8,.2,1) both;
  }
  @keyframes q-in { from { opacity: 0; transform: translateY(8px) scale(.98); } }
  .q-badge {
    width: 60px; height: 60px; margin: 0 auto 18px;
    border-radius: 18px; display: flex; align-items: center; justify-content: center;
    background: linear-gradient(135deg, #6d3bd4, #8f5cf0);
    box-shadow: 0 10px 24px rgba(109, 59, 212, .34);
  }
  .q-badge.error { background: linear-gradient(135deg, #e0435b, #f2685f);
    box-shadow: 0 10px 24px rgba(224, 67, 91, .32); }
  .q-glyph { color: #fff; font-size: 30px; font-weight: 800; line-height: 1; }
  .q-spin {
    width: 24px; height: 24px; border-radius: 50%;
    border: 3px solid rgba(255,255,255,.45); border-top-color: #fff;
    animation: q-rot .7s linear infinite;
  }
  @keyframes q-rot { to { transform: rotate(360deg); } }
  h1 { margin: 0 0 6px; font-size: 18px; font-weight: 700; letter-spacing: -.01em; }
  p { margin: 0; font-size: 13px; color: #7a7288; line-height: 1.5; word-break: break-word; }
  .q-brand { margin-top: 20px; font-size: 11px; letter-spacing: .16em;
    text-transform: uppercase; color: #b3a9c8; font-weight: 700; }
</style>
</head>
<body>
  <div class="q-card">
    <div class="q-badge __STATE__">__ICON__</div>
    <h1>__TITLE__</h1>
    <p>__SUBTITLE__</p>
    <div class="q-brand">Quberi</div>
  </div>
  <script id="quberi-msg" type="application/json">__DATA__</script>
  <script>
    (function () {
      var msg = {};
      try { msg = JSON.parse(document.getElementById('quberi-msg').textContent); } catch (e) {}
      try { if (window.opener) window.opener.postMessage(msg, '*'); } catch (e) {}
      __CLOSEJS__
    })();
  </script>
</body>
</html>"""

    return (
        template.replace("__STATE__", state)
        .replace("__ICON__", icon_html)
        .replace("__TITLE__", title)
        .replace("__SUBTITLE__", subtitle)
        .replace("__DATA__", data)
        .replace("__CLOSEJS__", close_js)
    )


@auth_router.get(
    "/feishu/callback",
    summary="Feishu SSO redirect target — exchanges code and signs in",
)
async def feishu_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    session: AsyncSession = Depends(get_db_session),
):
    from fastapi.responses import HTMLResponse

    if error:
        return HTMLResponse(
            _oauth_popup_html(source="quberi-feishu-login", ok=False, payload={"message": f"Feishu: {error}"})
        )
    if not code or not state:
        return HTMLResponse(
            _oauth_popup_html(source="quberi-feishu-login", ok=False, payload={"message": "missing code/state"}),
            status_code=400,
        )

    # Branch on the state's purpose: linking an already-signed-in account,
    # or a fresh login/signup.
    try:
        purpose = feishu_login_service.state_purpose(state)
    except feishu_login_service.FeishuLoginError as exc:
        return HTMLResponse(
            _oauth_popup_html(source="quberi-feishu-login", ok=False, payload={"message": str(exc)}),
            status_code=400,
        )

    if purpose == "feishu_link":
        try:
            await feishu_login_service.complete_link(
                session,
                code=code,
                state=state,
                redirect_uri=_feishu_login_redirect_uri(),
            )
        except feishu_login_service.FeishuLoginError as exc:
            return HTMLResponse(
                _oauth_popup_html(source="quberi-feishu-login", ok=False, payload={"message": str(exc)}),
                status_code=400,
            )
        except Exception as exc:  # noqa: BLE001
            return HTMLResponse(
                _oauth_popup_html(source="quberi-feishu-login", ok=False, payload={"message": f"unexpected: {exc}"}),
                status_code=500,
            )
        return HTMLResponse(
            _oauth_popup_html(source="quberi-feishu-login", ok=True, payload={"linked": True})
        )

    try:
        outcome = await feishu_login_service.complete_login(
            session,
            code=code,
            state=state,
            redirect_uri=_feishu_login_redirect_uri(),
        )
    except feishu_login_service.FeishuLoginError as exc:
        return HTMLResponse(
            _oauth_popup_html(source="quberi-feishu-login", ok=False, payload={"message": str(exc)}),
            status_code=400,
        )
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(
            _oauth_popup_html(source="quberi-feishu-login", ok=False, payload={"message": f"unexpected: {exc}"}),
            status_code=500,
        )

    # New Feishu user → no account yet. Hand back a bind ticket so the client
    # can collect + verify an email before we create/link the account.
    if outcome.resolved is None:
        return HTMLResponse(
            _oauth_popup_html(source="quberi-feishu-login", 
                ok=True,
                payload={
                    "pending": "bind_email",
                    "ticket": outcome.bind_ticket,
                    "feishu_name": outcome.feishu_name,
                    "feishu_email": outcome.feishu_email,
                },
            )
        )

    resolved = outcome.resolved
    import uuid

    token = issue_session(
        identity_id=uuid.UUID(resolved.identity_id),
        user_id=uuid.UUID(resolved.user_id),
        organization_id=uuid.UUID(resolved.organization_id),
        email=resolved.email,
    )
    org_role = await authz_service.get_direct_org_role(
        session,
        organization_id=uuid.UUID(resolved.organization_id),
        user_id=uuid.UUID(resolved.user_id),
    )
    return HTMLResponse(
        _oauth_popup_html(source="quberi-feishu-login", 
            ok=True,
            payload={
                "token": token,
                "user": {
                    "user_id": resolved.user_id,
                    "email": resolved.email,
                    "display_name": resolved.display_name,
                    "organization_id": resolved.organization_id,
                    "organization_slug": resolved.organization_slug,
                    "organization_name": resolved.organization_name,
                    "organization_tier": resolved.organization_tier,
                    "is_guest": False,
                    "org_role": org_role,
                },
            },
        )
    )


@auth_router.post(
    "/feishu/bind/request-code",
    response_model=RequestCodeResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send an email code to bind after Feishu consent",
)
async def feishu_bind_request_code(
    payload: FeishuBindRequestCode,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> RequestCodeResponse:
    ip = request.client.host if request.client else None
    try:
        await feishu_login_service.request_email_code_for_bind(
            session, ticket=payload.ticket, email=str(payload.email), ip_address=ip
        )
    except feishu_login_service.FeishuLoginError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return RequestCodeResponse(ok=True, message=_GENERIC_SENT)


@auth_router.post(
    "/feishu/bind/verify",
    response_model=VerifyCodeResponse,
    summary="Verify the email code, then create-or-link the Feishu account",
)
async def feishu_bind_verify(
    payload: FeishuBindVerify,
    session: AsyncSession = Depends(get_db_session),
) -> VerifyCodeResponse:
    try:
        resolved = await feishu_login_service.bind_email_and_resolve(
            session, ticket=payload.ticket, email=str(payload.email), code=payload.code
        )
    except feishu_login_service.FeishuLoginError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except verification_service.VerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    import uuid

    token = issue_session(
        identity_id=uuid.UUID(resolved.identity_id),
        user_id=uuid.UUID(resolved.user_id),
        organization_id=uuid.UUID(resolved.organization_id),
        email=resolved.email,
    )
    org_role = await authz_service.get_direct_org_role(
        session,
        organization_id=uuid.UUID(resolved.organization_id),
        user_id=uuid.UUID(resolved.user_id),
    )
    return VerifyCodeResponse(
        token=token,
        user=SessionUser(
            user_id=uuid.UUID(resolved.user_id),
            email=resolved.email,
            display_name=resolved.display_name,
            organization_id=uuid.UUID(resolved.organization_id),
            organization_slug=resolved.organization_slug,
            organization_name=resolved.organization_name,
            organization_tier=resolved.organization_tier,
            org_role=org_role,
        ),
    )


@auth_router.post(
    "/feishu/bind/phone/request-code",
    response_model=RequestCodeResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send an SMS code to bind after Feishu consent",
)
async def feishu_bind_phone_request_code(
    payload: FeishuBindPhoneRequestCode,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> RequestCodeResponse:
    ip = request.client.host if request.client else None
    try:
        await feishu_login_service.request_phone_code_for_bind(
            session, ticket=payload.ticket, phone=payload.phone, ip_address=ip
        )
    except feishu_login_service.FeishuLoginError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return RequestCodeResponse(ok=True, message=_PHONE_SENT)


@auth_router.post(
    "/feishu/bind/phone/verify",
    response_model=VerifyCodeResponse,
    summary="Verify the SMS code, then create-or-link the Feishu account by phone",
)
async def feishu_bind_phone_verify(
    payload: FeishuBindPhoneVerify,
    session: AsyncSession = Depends(get_db_session),
) -> VerifyCodeResponse:
    try:
        resolved = await feishu_login_service.bind_phone_and_resolve(
            session, ticket=payload.ticket, phone=payload.phone, code=payload.code
        )
    except feishu_login_service.FeishuLoginError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except verification_service.VerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    import uuid

    token = issue_session(
        identity_id=uuid.UUID(resolved.identity_id),
        user_id=uuid.UUID(resolved.user_id),
        organization_id=uuid.UUID(resolved.organization_id),
        email=resolved.email,
    )
    org_role = await authz_service.get_direct_org_role(
        session,
        organization_id=uuid.UUID(resolved.organization_id),
        user_id=uuid.UUID(resolved.user_id),
    )
    return VerifyCodeResponse(
        token=token,
        user=SessionUser(
            user_id=uuid.UUID(resolved.user_id),
            email=resolved.email,
            display_name=resolved.display_name,
            organization_id=uuid.UUID(resolved.organization_id),
            organization_slug=resolved.organization_slug,
            organization_name=resolved.organization_name,
            organization_tier=resolved.organization_tier,
            org_role=org_role,
        ),
    )


# ---------------------------------------------------------------------------
# Google SSO login
# ---------------------------------------------------------------------------


def _google_login_redirect_uri() -> str:
    base = get_settings().public_base_url
    if not base:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="QUBERI_PUBLIC_BASE_URL is not configured; Google login "
            "needs a public callback URL.",
        )
    return f"{base.rstrip('/')}/api/v1/auth/google/callback"


class GoogleAuthorizeUrlResponse(BaseModel):
    authorize_url: str


class GoogleAuthorizeUrlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # ``popup`` (default, backwards compatible): the page opens Google in a
    # small window and the callback postMessage's the result back.
    # ``redirect``: the whole tab goes to Google and the callback sends the
    # browser back to the app with the result in the URL fragment.
    mode: str = "popup"


@auth_router.post(
    "/google/authorize-url",
    response_model=GoogleAuthorizeUrlResponse,
    summary="Mint a Google SSO consent URL (login/signup)",
)
async def google_authorize_url(
    payload: Optional[GoogleAuthorizeUrlRequest] = None,
) -> GoogleAuthorizeUrlResponse:
    mode = payload.mode if payload is not None else "popup"
    try:
        url = google_login_service.build_login_authorize_url(
            redirect_uri=_google_login_redirect_uri(), flow=mode
        )
    except google_login_service.GoogleLoginError as exc:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED, detail=str(exc)
        ) from exc
    return GoogleAuthorizeUrlResponse(authorize_url=url)


def _google_result_response(
    *,
    flow: str,
    ok: bool,
    payload: dict,
    status_code: int = 200,
):
    """Hand a Google login/link outcome back to the browser.

    ``popup`` flow: the branded status page that ``postMessage``s the opener.
    ``redirect`` flow: a 302 to the app's origin with the outcome in the URL
    *fragment* (``#google_login=<base64url json>`` / ``#google_login_error=``).
    A fragment is never sent to any server nor written to access logs, and
    the SPA strips it from the address bar as soon as it has read it. The
    redirect target is the configured web origin — never taken from the
    request — so this cannot be turned into an open redirect.
    """

    from fastapi.responses import HTMLResponse, RedirectResponse

    if flow != google_login_service.FLOW_REDIRECT:
        return HTMLResponse(
            _oauth_popup_html(source="quberi-google-login", ok=ok, payload=payload),
            status_code=status_code,
        )

    import base64
    import json as _json
    from urllib.parse import quote

    # The *web app's* origin — not ``public_base_url``, which is the API's
    # (the OAuth callback lives there, but the page to land on does not).
    base = get_settings().web_origin
    if not base:
        # Cannot redirect anywhere safe; fall back to the status page.
        return HTMLResponse(
            _oauth_popup_html(source="quberi-google-login", ok=ok, payload=payload),
            status_code=status_code,
        )
    target = base.rstrip("/") + "/"
    if ok:
        blob = base64.urlsafe_b64encode(
            _json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        return RedirectResponse(f"{target}#google_login={blob}", status_code=302)
    message = quote(str(payload.get("message") or "Google sign-in failed"), safe="")
    return RedirectResponse(f"{target}#google_login_error={message}", status_code=302)


@auth_router.get(
    "/google/callback",
    summary="Google SSO redirect target — exchanges code and signs in",
)
async def google_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    session: AsyncSession = Depends(get_db_session),
):
    # Decided up front from the sealed state so *every* outcome below —
    # including errors — comes back the same way the sign-in started.
    flow = google_login_service.state_flow(state)

    def fail(message: str, status_code: int = 200):
        return _google_result_response(
            flow=flow, ok=False, payload={"message": message}, status_code=status_code
        )

    if error:
        return fail(f"Google: {error}")
    if not code or not state:
        return fail("missing code/state", 400)

    try:
        purpose = google_login_service.state_purpose(state)
    except google_login_service.GoogleLoginError as exc:
        return fail(str(exc), 400)

    if purpose == "google_link":
        try:
            await google_login_service.complete_link(
                session,
                code=code,
                state=state,
                redirect_uri=_google_login_redirect_uri(),
            )
        except google_login_service.GoogleLoginError as exc:
            return fail(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            return fail(f"unexpected: {exc}", 500)
        return _google_result_response(flow=flow, ok=True, payload={"linked": True})

    try:
        resolved = await google_login_service.complete_login(
            session,
            code=code,
            state=state,
            redirect_uri=_google_login_redirect_uri(),
        )
    except google_login_service.GoogleLoginError as exc:
        return fail(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return fail(f"unexpected: {exc}", 500)

    import uuid

    token = issue_session(
        identity_id=uuid.UUID(resolved.identity_id),
        user_id=uuid.UUID(resolved.user_id),
        organization_id=uuid.UUID(resolved.organization_id),
        email=resolved.email,
    )
    org_role = await authz_service.get_direct_org_role(
        session,
        organization_id=uuid.UUID(resolved.organization_id),
        user_id=uuid.UUID(resolved.user_id),
    )
    return _google_result_response(
        flow=flow,
        ok=True,
        payload={
            "token": token,
            "user": {
                "user_id": resolved.user_id,
                "email": resolved.email,
                "display_name": resolved.display_name,
                "organization_id": resolved.organization_id,
                "organization_slug": resolved.organization_slug,
                "organization_name": resolved.organization_name,
                "organization_tier": resolved.organization_tier,
                "is_guest": False,
                "org_role": org_role,
            },
        },
    )


# ---------------------------------------------------------------------------
# Switch active organization (re-issues a session token for a different
# org the caller already belongs to)
# ---------------------------------------------------------------------------


class SwitchOrganizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: uuid.UUID


@auth_router.post(
    "/switch-organization",
    response_model=VerifyCodeResponse,
    summary="Re-issue a session bound to a different organization you belong to",
)
async def switch_organization(
    payload: SwitchOrganizationRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> VerifyCodeResponse:
    if caller.is_guest:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="guests cannot switch organizations",
        )
    try:
        target_user = await org_membership_service.resolve_switch_target(
            session,
            identity_id=caller.user.identity_id,
            organization_id=payload.organization_id,
        )
    except org_membership_service.NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc

    org = (
        await session.execute(
            select(Organization).where(Organization.id == payload.organization_id)
        )
    ).scalar_one()
    org_role = await authz_service.get_direct_org_role(
        session,
        organization_id=org.id,
        user_id=target_user.id,
    )
    await session.commit()

    token = issue_session(
        identity_id=caller.user.identity_id,
        user_id=target_user.id,
        organization_id=org.id,
        email=target_user.email,
    )
    return VerifyCodeResponse(
        token=token,
        user=SessionUser(
            user_id=target_user.id,
            email=target_user.email,
            display_name=target_user.display_name,
            organization_id=org.id,
            organization_slug=org.slug,
            organization_name=org.name,
            organization_tier=org.tier,
            org_role=org_role,
        ),
    )


@auth_router.get("/me", response_model=MeResponse, summary="Current session user")
async def me(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> MeResponse:
    org_role = (
        ""
        if caller.is_guest
        else await authz_service.get_direct_org_role(
            session,
            organization_id=caller.organization.id,
            user_id=caller.user.id,
        )
    )
    return MeResponse(
        user=SessionUser(
            user_id=caller.user.id,
            email=caller.user.email,
            display_name=caller.user.display_name,
            organization_id=caller.organization.id,
            organization_slug=caller.organization.slug,
            organization_name=caller.organization.name,
            organization_tier=caller.organization.tier,
            is_guest=caller.is_guest,
            org_role=org_role,
        )
    )


@auth_router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def logout() -> Response:
    # Stateless tokens: logout is a client-side drop. Endpoint exists so the
    # client has a stable call, and so a future server-side revocation list
    # can hook in here without a contract change.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Platform email settings (admin)
# ---------------------------------------------------------------------------


@email_settings_router.get(
    "/email-settings",
    response_model=EmailProvidersResponse,
    dependencies=[Depends(require_platform_admin)],
    summary="List configured SMTP providers (passwords never returned)",
)
async def get_email_settings(
    session: AsyncSession = Depends(get_db_session),
) -> EmailProvidersResponse:
    views = await email_settings_service.list_providers(session)
    return EmailProvidersResponse(
        providers=[
            EmailProvider(
                id=v.id,
                name=v.name,
                priority=v.priority,
                enabled=v.enabled,
                host=v.host,
                port=v.port,
                username=v.username,
                from_address=v.from_address,
                use_ssl=v.use_ssl,
                has_password=v.has_password,
            )
            for v in views
        ]
    )


@email_settings_router.post(
    "/email-settings/test",
    dependencies=[Depends(require_platform_admin)],
    summary="Send a test email, trying providers in failover order",
)
async def test_email_settings(
    payload: TestEmailRequest,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    configs = await email_settings_service.enabled_smtp_configs(session)
    if not configs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no SMTP providers configured",
        )
    try:
        used = await send_email_with_failover(
            configs,
            to_address=str(payload.to),
            subject="Quberi SMTP 测试 / test",
            body_text="这是一封来自 Quberi 的测试邮件。\nThis is a Quberi SMTP test email.",
        )
    except EmailSendError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"send failed: {exc}",
        ) from exc
    return {"ok": True, "provider": used.name}
