"""Pydantic contracts for the offboarding checklist and batch handover."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class TransferSpacesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_owner_user_id: uuid.UUID
    # Capped here only so a huge array is not read at all; the real limit (100, after
    # removing duplicates) and the empty case are answered by the service with a code.
    space_ids: list[uuid.UUID] = Field(max_length=500)


class PersonRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    display_name: str


class ChecklistMember(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    display_name: str
    email: str
    status: str
    role_code: str


class OwnedSpaceItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    space_id: uuid.UUID
    name: str
    owner_type: str
    status: str
    other_owners: list[PersonRef]
    needs_transfer: bool
    pending_review_count: int


class ReviewerSpaceItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    space_id: uuid.UUID
    name: str
    pending_review_count: int
    other_reviewers: int


class PendingUploadItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    document_id: uuid.UUID
    name: str
    size_bytes: int
    created_at: datetime
    space_id: uuid.UUID
    space_name: str
    reviewer_count: int


class Impact(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    oauth_grants: int
    feishu_tokens: int
    feishu_sources: int
    private_model_bindings: int
    personal_spaces: int
    org_selections_referencing_private_keys: list[Literal["chat", "embedding"]]


class OffboardingChecklist(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    member: ChecklistMember
    personal_space_status: Optional[str]
    owned_spaces: list[OwnedSpaceItem]
    owned_spaces_total: int
    needs_transfer_total: int
    reviewer_spaces: list[ReviewerSpaceItem]
    reviewer_spaces_total: int
    uploads_as_uploader: list[PendingUploadItem]
    uploads_as_uploader_total: int
    impact: Impact


class TransferItemResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    space_id: uuid.UUID
    status: Literal["transferred", "already_done", "skipped", "failed"]
    code: Optional[str] = None
    pending_review_count: int = 0


class TransferSpacesResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    new_owner_user_id: uuid.UUID
    results: list[TransferItemResult]
    transferred: int
    already_done: int
    skipped: int
    failed: int
