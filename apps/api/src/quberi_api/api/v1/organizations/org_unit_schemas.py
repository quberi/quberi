"""Pydantic contracts for the org-unit API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

OrgUnitType = Literal[
    "department",
    "team",
    "business_unit",
    "legal_entity",
    "cost_center",
    "custom",
]


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class CreateOrgUnitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    type: OrgUnitType = "department"
    parent_id: Optional[uuid.UUID] = None
    code: Optional[str] = Field(default=None, max_length=64)
    manager_user_id: Optional[uuid.UUID] = None
    attributes: dict = Field(default_factory=dict)


class UpdateOrgUnitRequest(BaseModel):
    """PATCH payload — every field is optional.

    ``clear_manager`` explicitly detaches the manager (differs from *not
    sending* ``manager_user_id`` which means "keep it").
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    type: Optional[OrgUnitType] = None
    code: Optional[str] = Field(default=None, max_length=64)
    manager_user_id: Optional[uuid.UUID] = None
    clear_manager: bool = False
    attributes: Optional[dict] = None


class MoveOrgUnitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_parent_id: Optional[uuid.UUID] = None


class AssignUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID
    make_primary: bool = False


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class OrgUnitSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    parent_id: Optional[uuid.UUID]
    type: str
    name: str
    code: Optional[str]
    manager_user_id: Optional[uuid.UUID]
    status: str
    sync_state: str
    created_at: datetime
    updated_at: datetime


class OrgUnitDetail(OrgUnitSummary):
    attributes: dict
    ancestor_ids: list[uuid.UUID] = Field(default_factory=list)


class OrgUnitTreeItem(OrgUnitSummary):
    """Row shape used by the flat-tree list endpoint."""

    ancestor_ids: list[uuid.UUID] = Field(default_factory=list)


class OrgUnitTree(BaseModel):
    items: list[OrgUnitTreeItem]
    total: int


class OrgUnitMember(BaseModel):
    membership_id: uuid.UUID
    user_id: uuid.UUID
    display_name: str
    email: str
    status: str
    is_primary: bool


class OrgUnitMemberList(BaseModel):
    items: list[OrgUnitMember]
    total: int
