"""Position endpoints — self-managed job-role definitions + occupancy."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.api.v1.organizations.position_schemas import (
    AssignOccupantRequest,
    CreatePositionRequest,
    OccupantList,
    OccupantView,
    PositionDetail,
    PositionList,
    PositionListItem,
    UpdatePositionRequest,
)
from quberi_api.services import positions as positions_service
from quberi_api.services.authz import caller_has_org_role

router = APIRouter(
    prefix="/organizations/current/positions",
    tags=["organization-positions"],
)

_WRITE_ROLES = ("owner", "admin")


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------


def _detail(view: positions_service.PositionView) -> PositionDetail:
    return PositionDetail.model_validate(view.position).model_copy(
        update={
            "current_occupants": view.current_occupants,
            "description": view.position.description,
            "attributes": view.position.attributes,
        }
    )


def _list_item(view: positions_service.PositionView) -> PositionListItem:
    return PositionListItem.model_validate(view.position).model_copy(
        update={"current_occupants": view.current_occupants}
    )


def _occupant(view: positions_service.PositionOccupantView) -> OccupantView:
    return OccupantView(
        tenure_id=view.tenure_id,
        user_id=view.user.id,
        display_name=view.user.display_name,
        email=view.user.email,
        status=view.user.status,
        is_primary=view.is_primary,
        start_date=view.start_date,
        end_date=view.end_date,
    )


async def _require_org_admin(caller: CallerContext, session: AsyncSession) -> None:
    allowed = await caller_has_org_role(
        session,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
        allowed_codes=_WRITE_ROLES,
    )
    # Release the auto-began read txn so the service's session.begin() runs
    # cleanly (same pattern as members / org-units routers).
    await session.commit()
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only organisation owners or admins can modify positions",
        )


# ---------------------------------------------------------------------------
# Definition endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=PositionList,
    summary="List positions in the caller's organisation",
)
async def list_positions(
    include_archived: bool = False,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> PositionList:
    views = await positions_service.list_positions(
        session,
        organization_id=caller.organization.id,
        include_archived=include_archived,
    )
    items = [_list_item(v) for v in views]
    return PositionList(items=items, total=len(items))


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=PositionDetail,
    summary="Create a position (owner/admin only)",
)
async def create_position(
    payload: CreatePositionRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> PositionDetail:
    await _require_org_admin(caller, session)
    view = await positions_service.create_position(
        session,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
        name=payload.name,
        code=payload.code,
        organization_unit_id=payload.organization_unit_id,
        reports_to_position_id=payload.reports_to_position_id,
        job_family=payload.job_family,
        grade=payload.grade,
        headcount_budget=payload.headcount_budget,
        description=payload.description,
        attributes=payload.attributes,
    )
    return _detail(view)


@router.get(
    "/{position_id}",
    response_model=PositionDetail,
    summary="Fetch a single position",
)
async def get_position(
    position_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> PositionDetail:
    view = await positions_service.get_position(
        session,
        position_id=position_id,
        organization_id=caller.organization.id,
    )
    return _detail(view)


@router.patch(
    "/{position_id}",
    response_model=PositionDetail,
    summary="Update a position (owner/admin only)",
)
async def update_position(
    position_id: uuid.UUID,
    payload: UpdatePositionRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> PositionDetail:
    await _require_org_admin(caller, session)
    view = await positions_service.update_position(
        session,
        position_id=position_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
        name=payload.name,
        code=payload.code,
        organization_unit_id=payload.organization_unit_id,
        clear_org_unit=payload.clear_org_unit,
        reports_to_position_id=payload.reports_to_position_id,
        clear_reports_to=payload.clear_reports_to,
        job_family=payload.job_family,
        grade=payload.grade,
        headcount_budget=payload.headcount_budget,
        description=payload.description,
        status=payload.status,
        attributes=payload.attributes,
    )
    return _detail(view)


@router.post(
    "/{position_id}:archive",
    response_model=PositionDetail,
    summary="Archive a position (owner/admin only)",
)
async def archive_position(
    position_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> PositionDetail:
    await _require_org_admin(caller, session)
    view = await positions_service.archive_position(
        session,
        position_id=position_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
    )
    return _detail(view)


# ---------------------------------------------------------------------------
# Occupancy endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/{position_id}/occupants",
    response_model=OccupantList,
    summary="List users currently occupying a position",
)
async def list_occupants(
    position_id: uuid.UUID,
    include_ended: bool = False,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> OccupantList:
    views = await positions_service.list_occupants(
        session,
        position_id=position_id,
        organization_id=caller.organization.id,
        include_ended=include_ended,
    )
    items = [_occupant(v) for v in views]
    return OccupantList(items=items, total=len(items))


@router.post(
    "/{position_id}/occupants",
    status_code=status.HTTP_201_CREATED,
    response_model=OccupantView,
    summary="Assign a user to a position (owner/admin only)",
)
async def assign_occupant(
    position_id: uuid.UUID,
    payload: AssignOccupantRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> OccupantView:
    await _require_org_admin(caller, session)
    view = await positions_service.assign_user_to_position(
        session,
        position_id=position_id,
        user_id=payload.user_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
        is_primary=payload.is_primary,
        start_date_=payload.start_date,
    )
    return _occupant(view)


@router.delete(
    "/{position_id}/occupants/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="End a user's current tenure (owner/admin only)",
)
async def end_tenure(
    position_id: uuid.UUID,
    user_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await _require_org_admin(caller, session)
    await positions_service.end_position_tenure(
        session,
        position_id=position_id,
        user_id=user_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{position_id}/occupants/{user_id}:set-primary",
    response_model=OccupantView,
    summary="Mark this position as the user's primary (owner/admin only)",
)
async def set_primary(
    position_id: uuid.UUID,
    user_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> OccupantView:
    await _require_org_admin(caller, session)
    view = await positions_service.set_primary_position(
        session,
        position_id=position_id,
        user_id=user_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
    )
    return _occupant(view)


# Convenient alias for router assembly.
positions_router = router
