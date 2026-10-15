"""Pydantic contracts for the auth endpoints."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RequestCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: EmailStr


class RequestCodeResponse(BaseModel):
    # Deliberately generic — never reveals whether the email exists.
    ok: bool = True
    message: str


class VerifyCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: EmailStr
    code: str = Field(min_length=4, max_length=12)
    # Only used when this email has no account yet (login-or-signup); the
    # display name to seed the new personal account with. Ignored when
    # logging into an existing account.
    display_name: str | None = Field(default=None, max_length=120)


class SessionUser(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    organization_id: uuid.UUID
    organization_slug: str
    organization_name: str = ""
    # Product tier: "personal" (C-end, org UI hidden) or "team" (enterprise).
    organization_tier: str = "team"
    # True for anonymous guest sessions (demo org, read-only).
    is_guest: bool = False
    # Direct organisation-level role ("owner" / "admin" / "member" / "" for
    # none). Drives whether the client shows the admin-console entry point.
    # Always recomputed server-side on every login/me/switch — never trust
    # a stale client value for anything security-relevant.
    org_role: str = ""


class VerifyCodeResponse(BaseModel):
    token: str
    user: SessionUser


class MeResponse(BaseModel):
    user: SessionUser


class PhoneRequestCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    phone: str = Field(min_length=5, max_length=24)


class PhoneVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    phone: str = Field(min_length=5, max_length=24)
    code: str = Field(min_length=4, max_length=12)
    # Provide workspace_name for a team signup (tier=team, new phone only);
    # omit for the unified personal/login path. display_name is the owner's
    # name (required for team signup).
    workspace_name: str | None = Field(default=None, max_length=120)
    display_name: str | None = Field(default=None, max_length=120)


# ---------------------------------------------------------------------------
# Feishu SSO email binding (Feishu consent alone never creates an account)
# ---------------------------------------------------------------------------


class FeishuBindRequestCode(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Sealed ticket from the Feishu callback proving consent was completed.
    ticket: str = Field(min_length=8, max_length=8192)
    email: EmailStr


class FeishuBindVerify(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ticket: str = Field(min_length=8, max_length=8192)
    email: EmailStr
    code: str = Field(min_length=4, max_length=12)


class FeishuBindPhoneRequestCode(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ticket: str = Field(min_length=8, max_length=8192)
    phone: str = Field(min_length=5, max_length=24)


class FeishuBindPhoneVerify(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ticket: str = Field(min_length=8, max_length=8192)
    phone: str = Field(min_length=5, max_length=24)
    code: str = Field(min_length=4, max_length=12)


# ---------------------------------------------------------------------------
# Platform email settings (admin)
# ---------------------------------------------------------------------------


class EmailProvider(BaseModel):
    id: uuid.UUID
    name: str
    priority: int
    enabled: bool
    host: str
    port: int
    username: str | None
    from_address: str
    use_ssl: bool
    has_password: bool


class EmailProvidersResponse(BaseModel):
    providers: list[EmailProvider]


class TestEmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    to: EmailStr
