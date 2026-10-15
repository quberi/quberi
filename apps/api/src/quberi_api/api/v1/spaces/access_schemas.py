"""Pydantic contracts for the space access-bindings API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SubjectType = Literal[
    "user", "group", "organization_unit", "position", "organization"
]
SpaceRoleCode = Literal["owner", "admin", "editor", "viewer"]


class AddAccessBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_type: SubjectType
    subject_id: uuid.UUID
    role: SpaceRoleCode


class AccessBindingView(BaseModel):
    id: uuid.UUID
    subject_type: SubjectType
    subject_id: uuid.UUID
    subject_display_name: str
    subject_subtitle: Optional[str] = None
    role_code: SpaceRoleCode
    created_at: datetime


class AccessBindingList(BaseModel):
    items: list[AccessBindingView] = Field(default_factory=list)
    total: int


# ---------------------------------------------------------------------------
# Effective-members contract
# ---------------------------------------------------------------------------
#
# The hit paths mirror ``HitPath`` in ``services.authz`` — we don't reuse
# that Literal directly because the API layer needs a Pydantic-friendly
# alias.
HitPathCode = Literal[
    "direct",
    "position",
    "organization_unit",
    "organization_unit_ancestor",
    "group_via_user",
    "group_via_position",
    "group_via_organization_unit",
    "organization",
]

# ``subject_type`` at this layer includes ``user`` because a direct
# binding's subject is the user themselves (their own name — the UI can
# choose to elide it).
HitSubjectType = Literal[
    "user", "position", "organization_unit", "group", "organization"
]


class EffectiveHitPathView(BaseModel):
    hit_path: HitPathCode
    role_code: SpaceRoleCode
    subject_type: HitSubjectType
    subject_id: uuid.UUID
    subject_display_name: str = ""


class EffectiveMemberView(BaseModel):
    user_id: uuid.UUID
    display_name: str
    email: str
    effective_role: SpaceRoleCode
    hit_paths: list[EffectiveHitPathView] = Field(default_factory=list)


class EffectiveMemberList(BaseModel):
    items: list[EffectiveMemberView] = Field(default_factory=list)
    total: int
