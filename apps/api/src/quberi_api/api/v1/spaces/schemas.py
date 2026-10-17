"""Pydantic contracts for the space API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from quberi_api.api.v1.spaces.access_schemas import SpaceRoleCode, SubjectType

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class InitialGrant(BaseModel):
    """One access grant applied atomically when the space is created."""

    model_config = ConfigDict(extra="forbid")

    subject_type: SubjectType
    subject_id: uuid.UUID
    role: SpaceRoleCode


class CreateSpaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(
        min_length=1,
        max_length=80,
        # Loose upfront pattern; the service enforces the strict lowercase
        # rule so we get a helpful error message rather than pydantic's
        # generic ``string_pattern_mismatch``.
        pattern=r"^[A-Za-z0-9][A-Za-z0-9\- ]*[A-Za-z0-9]$",
    )
    description: Optional[str] = Field(default=None, max_length=2048)
    visibility: str = Field(default="private")
    trust_level: str = Field(default="guided")
    # ``"organization"`` creates an enterprise-owned space; only organisation
    # owners/admins may do that (enforced in the router).
    owner: Literal["user", "organization"] = "user"
    # Initial access: users, org units, groups, positions or the whole
    # organisation, each with a role. Applied in the same transaction as
    # the space itself.
    grants: list[InitialGrant] = Field(default_factory=list, max_length=100)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class SpaceSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    preset: str
    visibility: str
    trust_level: str
    status: str
    owner_type: str
    owner_id: uuid.UUID
    is_owner: bool = False
    # More than 0 when only some documents of this space are visible to the caller,
    # each shared with them individually (they cannot read the space as a whole).
    shared_only_documents: int = 0
    # Set only on the bilingual sample spaces ("en-US" / "zh-CN"); lets the
    # client show the one that matches the interface language. Ordinary
    # spaces have none.
    locale: Optional[str] = None
    updated_at: datetime


class SpaceDetail(SpaceSummary):
    organization_id: uuid.UUID
    description: Optional[str] = None
    parent_space_id: Optional[uuid.UUID] = None
    inheritance_policy: dict
    attributes: dict
    created_at: datetime


class SpaceList(BaseModel):
    items: list[SpaceSummary]
    total: int
