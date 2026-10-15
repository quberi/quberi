"""Pydantic contracts for the position API."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

PositionStatus = Literal["active", "frozen", "archived"]


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class CreatePositionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    code: Optional[str] = Field(default=None, max_length=64)
    organization_unit_id: Optional[uuid.UUID] = None
    reports_to_position_id: Optional[uuid.UUID] = None
    job_family: Optional[str] = Field(default=None, max_length=64)
    grade: Optional[str] = Field(default=None, max_length=32)
    headcount_budget: int = Field(default=1, ge=0)
    description: Optional[str] = Field(default=None, max_length=8000)
    attributes: dict = Field(default_factory=dict)


class UpdatePositionRequest(BaseModel):
    """PATCH payload — every field is optional.

    ``clear_org_unit`` / ``clear_reports_to`` explicitly detach the field
    (differ from *not sending* the id which means "keep it").
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    code: Optional[str] = Field(default=None, max_length=64)
    organization_unit_id: Optional[uuid.UUID] = None
    clear_org_unit: bool = False
    reports_to_position_id: Optional[uuid.UUID] = None
    clear_reports_to: bool = False
    job_family: Optional[str] = Field(default=None, max_length=64)
    grade: Optional[str] = Field(default=None, max_length=32)
    headcount_budget: Optional[int] = Field(default=None, ge=0)
    description: Optional[str] = Field(default=None, max_length=8000)
    status: Optional[Literal["active", "frozen"]] = None
    attributes: Optional[dict] = None


class AssignOccupantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID
    is_primary: bool = False
    start_date: Optional[date] = None


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class PositionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    code: Optional[str]
    organization_unit_id: Optional[uuid.UUID]
    reports_to_position_id: Optional[uuid.UUID]
    job_family: Optional[str]
    grade: Optional[str]
    headcount_budget: int
    status: PositionStatus
    sync_state: str
    created_at: datetime
    updated_at: datetime


class PositionDetail(PositionSummary):
    description: Optional[str] = None
    attributes: dict = Field(default_factory=dict)
    current_occupants: int = 0


class PositionListItem(PositionSummary):
    current_occupants: int = 0


class PositionList(BaseModel):
    items: list[PositionListItem]
    total: int


class OccupantView(BaseModel):
    tenure_id: uuid.UUID
    user_id: uuid.UUID
    display_name: str
    email: str
    status: str
    is_primary: bool
    start_date: date
    end_date: Optional[date]


class OccupantList(BaseModel):
    items: list[OccupantView]
    total: int
