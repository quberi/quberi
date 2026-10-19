"""Pydantic contracts for the tenant-scoped organisation API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field  # noqa: F401 (Field used below)

# Only organisation-level assignable roles. ``editor`` / ``viewer`` are
# space-level and rejected here — those get granted via the Space members
# API in a later slice.
InvitableRole = Literal["admin", "member"]


class InviteMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: EmailStr
    display_name: str = Field(min_length=1, max_length=200)
    role: InvitableRole = "member"


class SetMemberRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    role: InvitableRole


class DirectManagerRef(BaseModel):
    user_id: uuid.UUID
    display_name: str


class MemberSummary(BaseModel):
    """One row in the members list."""

    id: uuid.UUID
    display_name: str
    email: str
    status: str
    role_code: str
    is_pending: bool
    created_at: datetime
    updated_at: datetime
    primary_org_unit_id: Optional[uuid.UUID] = None
    primary_org_unit_name: Optional[str] = None
    primary_position_id: Optional[uuid.UUID] = None
    primary_position_name: Optional[str] = None
    direct_managers: list[DirectManagerRef] = Field(default_factory=list)


class MemberList(BaseModel):
    items: list[MemberSummary]
    total: int


class DeactivationEffects(BaseModel):
    """What deactivating a member did (all zero when nothing changed)."""

    oauth_grants_revoked: int
    feishu_tokens_removed: int
    feishu_sources_paused: int
    private_model_bindings_removed: int
    org_model_selections_removed: int
    other_org_selections_invalidated: int
    personal_spaces_frozen: int


class DeactivationResponse(BaseModel):
    member: MemberSummary
    changed: bool
    effects: DeactivationEffects


class ReactivationResponse(BaseModel):
    member: MemberSummary
    changed: bool
    personal_spaces_unfrozen: int
