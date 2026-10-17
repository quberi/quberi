"""Asking a space's owner for access.

* POST /api/v1/access-requests                      ask (organisation owners/admins only)
* GET  /api/v1/access-requests?scope=to_decide      requests waiting for me as a space owner
* GET  /api/v1/access-requests?scope=mine           requests I made
* POST /api/v1/access-requests/{id}/approve|reject  decide (an owner of that space who can act)
* POST /api/v1/access-requests/{id}/cancel          withdraw my own pending request
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.services import access_requests as service

router = APIRouter(prefix="/access-requests", tags=["access-requests"])


class CreateAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_id: uuid.UUID
    role: Literal["viewer", "editor"]
    reason: str = Field(min_length=1, max_length=service.MAX_REASON_CHARS)


class DecideAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(default="", max_length=service.MAX_NOTE_CHARS)


class AccessRequestView(BaseModel):
    id: uuid.UUID
    space_id: uuid.UUID
    space_name: str
    requester_user_id: uuid.UUID
    requester_name: str
    requester_email: str
    role: str
    reason: str
    status: str
    decided_by_name: Optional[str] = None
    decision_note: Optional[str] = None
    created_at: datetime
    decided_at: Optional[datetime] = None


class AccessRequestList(BaseModel):
    items: list[AccessRequestView] = Field(default_factory=list)
    total: int


def _to_response(view: service.RequestView) -> AccessRequestView:
    r = view.request
    return AccessRequestView(
        id=r.id,
        space_id=r.space_id,
        space_name=view.space_name,
        requester_user_id=r.requester_user_id,
        requester_name=view.requester_name,
        requester_email=view.requester_email,
        role=r.role_code,
        reason=r.reason,
        status=r.status,
        decided_by_name=view.decided_by_name,
        decision_note=r.decision_note,
        created_at=r.created_at,
        decided_at=r.decided_at,
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=AccessRequestView,
             summary="Ask a space's owner for access")
async def create_access_request(
    payload: CreateAccessRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AccessRequestView:
    organization_id, user_id = caller.organization.id, caller.user.id
    view = await service.create_request(
        session,
        organization_id=organization_id,
        requester_user_id=user_id,
        space_id=payload.space_id,
        role_code=payload.role,
        reason=payload.reason,
    )
    return _to_response(view)


@router.get("", response_model=AccessRequestList, summary="Requests waiting for me, or that I made")
async def list_access_requests(
    scope: Literal["to_decide", "mine"] = Query(default="to_decide"),
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AccessRequestList:
    organization_id, user_id = caller.organization.id, caller.user.id
    if scope == "mine":
        views = await service.list_mine(session, organization_id=organization_id, user_id=user_id)
    else:
        views = await service.list_to_decide(
            session, organization_id=organization_id, user_id=user_id
        )
    items = [_to_response(v) for v in views]
    return AccessRequestList(items=items, total=len(items))


async def _decide(
    request_id: uuid.UUID,
    approve: bool,
    payload: DecideAccessRequest,
    caller: CallerContext,
    session: AsyncSession,
) -> AccessRequestView:
    organization_id, user_id = caller.organization.id, caller.user.id
    await session.rollback()  # end the read transaction the caller lookup opened
    view = await service.decide(
        session,
        organization_id=organization_id,
        actor_user_id=user_id,
        request_id=request_id,
        approve=approve,
        note=payload.note,
    )
    return _to_response(view)


@router.post("/{request_id}/approve", response_model=AccessRequestView,
             summary="Approve a request: grants the role (owners of the space only)")
async def approve_access_request(
    request_id: uuid.UUID,
    payload: DecideAccessRequest = DecideAccessRequest(),
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AccessRequestView:
    return await _decide(request_id, True, payload, caller, session)


@router.post("/{request_id}/reject", response_model=AccessRequestView,
             summary="Reject a request (owners of the space only)")
async def reject_access_request(
    request_id: uuid.UUID,
    payload: DecideAccessRequest = DecideAccessRequest(),
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AccessRequestView:
    return await _decide(request_id, False, payload, caller, session)


@router.post("/{request_id}/cancel", response_model=AccessRequestView,
             summary="Withdraw my own pending request")
async def cancel_access_request(
    request_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AccessRequestView:
    organization_id, user_id = caller.organization.id, caller.user.id
    await session.rollback()
    view = await service.cancel(
        session, organization_id=organization_id, actor_user_id=user_id, request_id=request_id
    )
    return _to_response(view)


access_requests_router = router
