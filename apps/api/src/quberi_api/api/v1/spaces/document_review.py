"""Upload approval.

* PUT  /api/v1/spaces/{space_id}/upload-review          turn it on or off (space owner only)
* GET  /api/v1/spaces/{space_id}/review/documents       uploads waiting (or refused), for reviewers
* POST /api/v1/spaces/{space_id}/review/decide          approve or reject, one or many
* GET  /api/v1/spaces/{space_id}/review/versions        new versions waiting or refused (reviewers)
* POST /api/v1/spaces/{space_id}/review/versions/decide approve or reject new versions
* GET  /api/v1/document-reviews/pending                 spaces I review that have uploads waiting
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.services.documents import review as service
from quberi_api.services.documents import version_review

router = APIRouter(tags=["document-review"])


class UploadReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class UploadReviewResponse(BaseModel):
    space_id: uuid.UUID
    enabled: bool


class ReviewItemView(BaseModel):
    id: uuid.UUID
    name: str
    mime_type: str
    size_bytes: int
    uploader_name: str
    created_at: datetime
    review_state: str
    review_note: Optional[str] = None
    reviewed_by_name: Optional[str] = None


class ReviewItemList(BaseModel):
    items: list[ReviewItemView] = Field(default_factory=list)
    total: int


class DecideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_ids: list[uuid.UUID] = Field(min_length=1, max_length=service.MAX_BATCH)
    action: Literal["approve", "reject"]
    note: str = Field(default="", max_length=service.MAX_NOTE_CHARS)


class DecideResponse(BaseModel):
    changed: list[uuid.UUID]


class VersionReviewItemView(BaseModel):
    document_id: uuid.UUID
    document_name: str
    version_number: int
    filename: str
    mime_type: str
    size_bytes: int
    uploader_name: str
    created_at: datetime
    # What the uploader said changed.
    note: Optional[str] = None
    review_state: str
    review_note: Optional[str] = None
    reviewed_by_name: Optional[str] = None


class VersionReviewItemList(BaseModel):
    items: list[VersionReviewItemView] = Field(default_factory=list)
    total: int


class VersionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: uuid.UUID
    version_number: int = Field(ge=1)


class DecideVersionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    versions: list[VersionTarget] = Field(min_length=1, max_length=service.MAX_BATCH)
    action: Literal["approve", "reject"]
    note: str = Field(default="", max_length=service.MAX_NOTE_CHARS)


class DecideVersionsResponse(BaseModel):
    changed: list[VersionTarget]


class PendingSpaceView(BaseModel):
    space_id: uuid.UUID
    space_name: str
    pending: int


class PendingSpaceList(BaseModel):
    items: list[PendingSpaceView] = Field(default_factory=list)
    total_pending: int


@router.put("/spaces/{space_id}/upload-review", response_model=UploadReviewResponse,
            summary="Turn upload approval on or off (space owner only)")
async def set_upload_review(
    space_id: uuid.UUID,
    payload: UploadReviewRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> UploadReviewResponse:
    organization_id, user_id = caller.organization.id, caller.user.id
    enabled = await service.set_requirement(
        session,
        organization_id=organization_id,
        actor_user_id=user_id,
        space_id=space_id,
        enabled=payload.enabled,
    )
    return UploadReviewResponse(space_id=space_id, enabled=enabled)


@router.get("/spaces/{space_id}/review/documents", response_model=ReviewItemList,
            summary="Uploads waiting for approval, or refused (owners and admins of the space)")
async def list_review_documents(
    space_id: uuid.UUID,
    state: Literal["pending", "rejected"] = Query(default="pending"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ReviewItemList:
    items, total = await service.list_for_review(
        session,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
        space_id=space_id,
        state=state,
        limit=limit,
        offset=offset,
    )
    return ReviewItemList(
        items=[ReviewItemView(**vars(i)) for i in items],
        total=total,
    )


@router.post("/spaces/{space_id}/review/decide", response_model=DecideResponse,
             summary="Approve (queues indexing) or reject (needs a reason) uploads")
async def decide_review(
    space_id: uuid.UUID,
    payload: DecideRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> DecideResponse:
    organization_id, user_id = caller.organization.id, caller.user.id
    await session.rollback()  # end the read transaction the caller lookup opened
    changed = await service.decide(
        session,
        organization_id=organization_id,
        actor_user_id=user_id,
        space_id=space_id,
        document_ids=payload.document_ids,
        approve=payload.action == "approve",
        note=payload.note,
    )
    return DecideResponse(changed=changed)


@router.get("/spaces/{space_id}/review/versions", response_model=VersionReviewItemList,
            summary="New versions waiting for approval, or refused (owners and admins)")
async def list_review_versions(
    space_id: uuid.UUID,
    state: Literal["pending", "rejected"] = Query(default="pending"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> VersionReviewItemList:
    items, total = await version_review.list_for_review(
        session,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
        space_id=space_id,
        state=state,
        limit=limit,
        offset=offset,
    )
    return VersionReviewItemList(
        items=[VersionReviewItemView(**vars(i)) for i in items], total=total
    )


@router.post("/spaces/{space_id}/review/versions/decide", response_model=DecideVersionsResponse,
             summary="Approve (the version becomes current and is indexed) or reject new versions")
async def decide_review_versions(
    space_id: uuid.UUID,
    payload: DecideVersionsRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> DecideVersionsResponse:
    organization_id, user_id = caller.organization.id, caller.user.id
    await session.rollback()  # end the read transaction the caller lookup opened
    changed = await version_review.decide(
        session,
        organization_id=organization_id,
        actor_user_id=user_id,
        space_id=space_id,
        targets=[(t.document_id, t.version_number) for t in payload.versions],
        approve=payload.action == "approve",
        note=payload.note,
    )
    return DecideVersionsResponse(
        changed=[VersionTarget(document_id=d, version_number=n) for d, n in changed]
    )


@router.get("/document-reviews/pending", response_model=PendingSpaceList,
            summary="Spaces I review that have uploads waiting")
async def pending_reviews(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> PendingSpaceList:
    spaces = await service.pending_inbox(
        session, organization_id=caller.organization.id, user_id=caller.user.id
    )
    return PendingSpaceList(
        items=[PendingSpaceView(space_id=s.space_id, space_name=s.space_name, pending=s.pending) for s in spaces],
        total_pending=sum(s.pending for s in spaces),
    )


document_review_router = router
