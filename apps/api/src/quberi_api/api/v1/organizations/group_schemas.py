"""Pydantic contracts for the group API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

GroupType = Literal["static", "dynamic", "security", "project"]
GroupMemberType = Literal["user", "organization_unit", "position", "service_account"]


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class CreateGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    type: GroupType = "static"
    description: Optional[str] = Field(default=None, max_length=1024)
    attributes: dict = Field(default_factory=dict)


class UpdateGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    type: Optional[GroupType] = None
    description: Optional[str] = Field(default=None, max_length=1024)
    attributes: Optional[dict] = None


class AddGroupMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member_type: GroupMemberType
    member_id: uuid.UUID


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class GroupSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    type: GroupType
    name: str
    description: Optional[str]
    status: str
    sync_state: str
    created_at: datetime
    updated_at: datetime


class GroupListItem(GroupSummary):
    member_count: int = 0


class GroupDetail(GroupSummary):
    attributes: dict = Field(default_factory=dict)
    member_count: int = 0


class GroupList(BaseModel):
    items: list[GroupListItem]
    total: int


class GroupMemberEntry(BaseModel):
    id: uuid.UUID  # the GroupMember row id
    member_type: GroupMemberType
    member_id: uuid.UUID
    display_name: str
    subtitle: Optional[str] = None
    created_at: datetime


class GroupMemberList(BaseModel):
    items: list[GroupMemberEntry]
    total: int


# ---------------------------------------------------------------------------
# Effective-members contract
# ---------------------------------------------------------------------------

GroupHitPathCode = Literal["direct", "via_position", "via_organization_unit"]
# ``subject_type`` uses ``user`` for the direct case (the user is their own
# subject), which mirrors ``EffectiveHitPathView`` on the space endpoint.
GroupHitSubjectType = Literal["user", "position", "organization_unit"]


class EffectiveGroupHitPathView(BaseModel):
    hit_path: GroupHitPathCode
    subject_type: GroupHitSubjectType
    subject_id: uuid.UUID
    subject_display_name: str = ""


class EffectiveGroupMemberView(BaseModel):
    user_id: uuid.UUID
    display_name: str
    email: str
    hit_paths: list[EffectiveGroupHitPathView] = Field(default_factory=list)


class EffectiveGroupMemberList(BaseModel):
    items: list[EffectiveGroupMemberView] = Field(default_factory=list)
    total: int


# ---------------------------------------------------------------------------
# Reverse lookup: which groups does a user belong to?
# ---------------------------------------------------------------------------


class UserGroupList(BaseModel):
    """Groups the user has membership in, via any literal path."""

    items: list[GroupListItem] = Field(default_factory=list)
    total: int
