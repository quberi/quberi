"""Cross-organisation membership for the signed-in identity.

* GET  /api/v1/organizations/mine — every org this identity belongs to
      (self-created or invited into).
* POST /api/v1/organizations      — create a new team organisation; the
      caller becomes its owner. Does not switch the caller's active
      session — call /api/v1/auth/switch-organization afterwards if you
      want to act inside it right away.

Both require ``require_caller`` (any authenticated user, any org/tier).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.services import org_membership as org_membership_service

router = APIRouter(prefix="/organizations", tags=["organizations"])


class MyOrganizationSummary(BaseModel):
    organization_id: uuid.UUID
    name: str
    slug: str
    tier: str
    is_current: bool
    is_pending: bool


class MyOrganizationList(BaseModel):
    items: list[MyOrganizationSummary]


class CreateTeamRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)


class CreateTeamResponse(BaseModel):
    organization_id: uuid.UUID
    name: str
    slug: str
    tier: str


@router.get(
    "/mine",
    response_model=MyOrganizationList,
    summary="List every organisation the signed-in identity belongs to",
)
async def list_mine(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> MyOrganizationList:
    orgs = await org_membership_service.list_my_organizations(
        session, identity_id=caller.user.identity_id
    )
    return MyOrganizationList(
        items=[
            MyOrganizationSummary(
                organization_id=o.organization_id,
                name=o.name,
                slug=o.slug,
                tier=o.tier,
                is_current=o.organization_id == caller.organization.id,
                is_pending=o.is_pending,
            )
            for o in orgs
        ]
    )


@router.post(
    "",
    response_model=CreateTeamResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new team organisation (caller becomes owner)",
)
async def create_team(
    payload: CreateTeamRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> CreateTeamResponse:
    if caller.is_guest:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="guests cannot create an organization",
        )
    try:
        result = await org_membership_service.create_team_organization(
            session,
            identity_id=caller.user.identity_id,
            owner_email=caller.user.email,
            owner_display_name=caller.user.display_name,
            name=payload.name,
        )
    except org_membership_service.InvalidInputError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return CreateTeamResponse(
        organization_id=result.organization_id,
        name=result.name,
        slug=result.slug,
        tier=result.tier,
    )


mine_router = router
