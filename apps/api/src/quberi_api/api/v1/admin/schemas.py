"""Pydantic contracts for the admin API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class BootstrapOrganizationRequest(BaseModel):
    """Payload for ``POST /api/v1/admin/organizations``."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(
        min_length=1,
        max_length=80,
        # Loose upfront check; the service applies the strict regex and lowercase
        # normalisation. Keeping this pattern permissive lets the service return
        # a friendlier "slug must be…" error rather than Pydantic's generic
        # ``string_pattern_mismatch``.
        pattern=r"^[A-Za-z0-9][A-Za-z0-9\- ]*[A-Za-z0-9]$",
    )
    owner_email: EmailStr
    owner_display_name: str = Field(min_length=1, max_length=200)
    default_locale: str = Field(default="en-US", max_length=16)
    timezone: str = Field(default="UTC", max_length=64)
    data_region: str = Field(default="local", max_length=32)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class BootstrapOrganizationResponse(BaseModel):
    """Return payload for a successful ``POST``.

    Callers get every id created in the transaction so they can persist them
    in their own system (identity provider, CRM, etc.) atomically with the
    org's creation.
    """

    organization_id: uuid.UUID
    identity_id: uuid.UUID
    user_id: uuid.UUID
    personal_space_id: uuid.UUID
    role_binding_id: uuid.UUID


class OrganizationSummary(BaseModel):
    """Compact row used in list responses."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    status: str
    data_region: str
    created_at: datetime


class OrganizationDetail(OrganizationSummary):
    """Full org row used for the detail endpoint."""

    default_locale: str
    timezone: str
    updated_at: datetime
    attributes: dict


class OrganizationList(BaseModel):
    items: list[OrganizationSummary]
    total: int
    next_cursor: Optional[str] = None
