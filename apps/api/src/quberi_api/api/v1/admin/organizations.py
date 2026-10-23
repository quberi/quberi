"""Platform-admin organisation management.

Endpoints
---------
POST /api/v1/admin/organizations
    Create a new tenant + owner + personal space in one transaction. Returns
    201 with every id produced by the transaction so the caller can hand them
    off to downstream systems atomically.

GET  /api/v1/admin/organizations
    List every organisation on this deployment, newest first. Simple
    limit-based pagination; a cursor is returned when more rows exist.

GET  /api/v1/admin/organizations/{organization_id}
    Single-organisation detail.

All three require ``Authorization: Bearer <QUBERI_PLATFORM_ADMIN_TOKEN>``.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.admin_errors import AdminApiException
from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import require_platform_admin
from quberi_api.api.v1.admin.schemas import (
    BootstrapOrganizationRequest,
    BootstrapOrganizationResponse,
    OrganizationDetail,
    OrganizationList,
    OrganizationSummary,
)
from quberi_api.domain.identity.models import Organization
from quberi_api.services.onboarding import bootstrap_organization

router = APIRouter(
    prefix="/admin/organizations",
    tags=["admin"],
    dependencies=[Depends(require_platform_admin)],
)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=BootstrapOrganizationResponse,
    summary="Create an organisation with an owner and personal space",
)
async def create_organization(
    payload: BootstrapOrganizationRequest,
    session: AsyncSession = Depends(get_db_session),
) -> BootstrapOrganizationResponse:
    result = await bootstrap_organization(
        session,
        name=payload.name,
        slug=payload.slug,
        owner_email=str(payload.owner_email),
        owner_display_name=payload.owner_display_name,
        default_locale=payload.default_locale,
        timezone=payload.timezone,
        data_region=payload.data_region,
    )
    return BootstrapOrganizationResponse(
        organization_id=result.organization_id,
        identity_id=result.identity_id,
        user_id=result.user_id,
        personal_space_id=result.personal_space_id,
        role_binding_id=result.role_binding_id,
    )


@router.get(
    "",
    response_model=OrganizationList,
    summary="List organisations",
)
async def list_organizations(
    limit: int = Query(default=50, ge=1, le=200),
    cursor: Optional[str] = Query(
        default=None,
        description="Opaque cursor returned by a previous page.",
    ),
    session: AsyncSession = Depends(get_db_session),
) -> OrganizationList:
    stmt = select(Organization).order_by(Organization.created_at.desc(), Organization.id.desc())

    # The cursor is just the last-seen ``created_at`` iso timestamp; we ship
    # something opaque to callers so we can change the strategy later.
    if cursor:
        stmt = stmt.where(Organization.created_at < cursor)

    stmt = stmt.limit(limit + 1)

    rows = list((await session.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    page = rows[:limit]

    total = (await session.execute(select(func.count()).select_from(Organization))).scalar_one()

    return OrganizationList(
        items=[OrganizationSummary.model_validate(row) for row in page],
        total=total,
        next_cursor=page[-1].created_at.isoformat() if has_more and page else None,
    )


@router.get(
    "/{organization_id}",
    response_model=OrganizationDetail,
    summary="Get a single organisation",
)
async def get_organization(
    organization_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
) -> OrganizationDetail:
    row = (
        await session.execute(select(Organization).where(Organization.id == organization_id))
    ).scalar_one_or_none()
    if row is None:
        raise AdminApiException(404, "admin.orgs.not_found", id=str(organization_id))
    return OrganizationDetail.model_validate(row)
