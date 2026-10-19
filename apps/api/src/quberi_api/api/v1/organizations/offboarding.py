"""Offboarding endpoints (organisation owner only).

* GET  /api/v1/organizations/current/members/{user_id}/offboarding
* POST /api/v1/organizations/current/members/{user_id}/offboarding/transfer-spaces
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.api.v1.organizations._guards import require_org_owner
from quberi_api.api.v1.organizations.offboarding_schemas import (
    OffboardingChecklist,
    TransferSpacesRequest,
    TransferSpacesResponse,
)
from quberi_api.services import offboarding as offboarding_service

offboarding_router = APIRouter(
    prefix="/organizations/current/members/{user_id}/offboarding", tags=["organizations"]
)


@offboarding_router.get(
    "",
    response_model=OffboardingChecklist,
    summary="What a member still holds: owned spaces, review duties, pending uploads",
)
async def get_offboarding_checklist(
    user_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> OffboardingChecklist:
    await require_org_owner(caller, session)
    checklist = await offboarding_service.build_checklist(
        session, organization_id=caller.organization.id, member_id=user_id
    )
    return OffboardingChecklist.model_validate(checklist)


@offboarding_router.post(
    "/transfer-spaces",
    response_model=TransferSpacesResponse,
    summary="Hand a deactivated member's collaboration spaces to a new owner",
)
async def transfer_member_spaces(
    user_id: uuid.UUID,
    payload: TransferSpacesRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> TransferSpacesResponse:
    await require_org_owner(caller, session)
    report = await offboarding_service.transfer_spaces(
        session,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
        member_id=user_id,
        new_owner_user_id=payload.new_owner_user_id,
        space_ids=payload.space_ids,
    )
    return TransferSpacesResponse.model_validate(report)
