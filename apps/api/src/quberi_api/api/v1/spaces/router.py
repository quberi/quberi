"""Tenant-scoped space endpoints.

Endpoints
---------
GET  /api/v1/spaces               → list spaces the caller can access
POST /api/v1/spaces               → create a collaborative space (creator owns it)
GET  /api/v1/spaces/{id}          → single space (404 if the caller can't see it)
POST /api/v1/spaces/{id}:archive  → archive a space (owner-only for now)

All four require the ``require_caller`` dependency, which today means the
platform admin bearer token plus ``X-Quberi-Organization`` +
``X-Quberi-User-Email`` headers. Real SSO replaces that scheme without
changing this router.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.api.v1.spaces.schemas import (
    CreateSpaceRequest,
    SpaceDetail,
    SpaceList,
    SpaceSummary,
)
from quberi_api.services import space_owners
from quberi_api.services import spaces as spaces_service
from quberi_api.services.authz import caller_has_org_role

router = APIRouter(prefix="/spaces", tags=["spaces"])


async def _require_org_admin(caller: CallerContext, session: AsyncSession) -> None:
    """403 unless the caller is a *direct* org owner/admin.

    Commits the read transaction it opens so the service layer's own
    ``session.begin()`` starts clean (same pattern as the other admin guards).
    """

    allowed = await caller_has_org_role(
        session,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
        allowed_codes=("owner", "admin"),
    )
    await session.commit()
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only organisation owners or admins can manage enterprise spaces",
        )


def _locale(view: spaces_service.SpaceView) -> str | None:
    value = (view.space.attributes or {}).get("locale")
    return value if isinstance(value, str) else None


def _summary(view: spaces_service.SpaceView) -> SpaceSummary:
    summary = SpaceSummary.model_validate(view.space)
    return summary.model_copy(
        update={
            "is_owner": view.is_owner,
            "locale": _locale(view),
            "shared_only_documents": view.shared_only_documents,
        }
    )


def _detail(view: spaces_service.SpaceView) -> SpaceDetail:
    detail = SpaceDetail.model_validate(view.space)
    return detail.model_copy(
        update={
            "is_owner": view.is_owner,
            "locale": _locale(view),
            "shared_only_documents": view.shared_only_documents,
        }
    )


@router.get(
    "",
    response_model=SpaceList,
    summary="List spaces the caller can access",
)
async def list_spaces(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> SpaceList:
    views = await spaces_service.list_spaces_for_user(
        session,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
        include_shared=True,
    )
    items = [_summary(view) for view in views]
    return SpaceList(items=items, total=len(items))


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SpaceDetail,
    summary="Create a collaborative space",
)
async def create_space(
    payload: CreateSpaceRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> SpaceDetail:
    if payload.owner == "organization":
        await _require_org_admin(caller, session)
    elif payload.grants:
        # Granting access to other people/groups at creation time is the
        # same capability as the access-bindings API, so a plain member
        # creating a personal-owned space keeps using that API instead.
        # Keep the surface small: initial grants are for enterprise spaces.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="initial grants are only supported for enterprise-owned spaces",
        )
    view = await spaces_service.create_collaborative_space(
        session,
        organization_id=caller.organization.id,
        creator_user_id=caller.user.id,
        name=payload.name,
        slug=payload.slug,
        description=payload.description,
        visibility=payload.visibility,
        trust_level=payload.trust_level,
        tier=caller.organization.tier,
        owner=payload.owner,
        grants=[
            spaces_service.SpaceGrant(
                subject_type=g.subject_type,
                subject_id=g.subject_id,
                role=g.role,
            )
            for g in payload.grants
        ],
    )
    return _detail(view)


@router.get(
    "/organization",
    response_model=SpaceList,
    summary="List every enterprise-owned space (org owner/admin only)",
)
async def list_organization_spaces(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> SpaceList:
    await _require_org_admin(caller, session)
    views = await spaces_service.list_organization_spaces(
        session, organization_id=caller.organization.id
    )
    unavailable = await space_owners.owners_unavailable_for_spaces(
        session,
        organization_id=caller.organization.id,
        space_ids=[v.space.id for v in views if v.space.status == "active"],
    )
    items = [
        _summary(view).model_copy(update={"owner_unavailable": view.space.id in unavailable})
        for view in views
    ]
    return SpaceList(items=items, total=len(items))


class TransferOwnerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_owner_user_id: uuid.UUID


class TransferOwnerResponse(BaseModel):
    space_id: uuid.UUID
    new_owner_user_id: uuid.UUID
    # "handover" (an owner passes it on) or "replacement" (the organisation owner names a
    # new one because no owner is available).
    mode: str


@router.post(
    "/{space_id}/owner",
    response_model=TransferOwnerResponse,
    summary="Hand a space to a new owner, or name one when the owners are gone",
)
async def transfer_space_owner(
    space_id: uuid.UUID,
    payload: TransferOwnerRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> TransferOwnerResponse:
    # An owner of the space hands it over; the organisation owner may replace owners that
    # can no longer act. The service decides which applies and refuses anyone else.
    organization_id, actor_user_id = caller.organization.id, caller.user.id
    await session.rollback()  # end the read transaction the caller lookup opened
    result = await space_owners.transfer_owner(
        session,
        space_id=space_id,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        new_owner_user_id=payload.new_owner_user_id,
    )
    return TransferOwnerResponse(
        space_id=result.space_id,
        new_owner_user_id=result.new_owner_user_id,
        mode=result.mode,
    )


@router.get(
    "/{space_id}",
    response_model=SpaceDetail,
    summary="Fetch one space",
)
async def get_space(
    space_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> SpaceDetail:
    view = await spaces_service.get_space_for_user(
        session,
        space_id=space_id,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
        include_shared=True,
    )
    return _detail(view)


@router.post(
    "/{space_id}:archive",
    response_model=SpaceDetail,
    summary="Archive a space (owner-only)",
)
async def archive_space(
    space_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> SpaceDetail:
    view = await spaces_service.archive_space(
        session,
        space_id=space_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
    )
    return _detail(view)
