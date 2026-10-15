"""Platform-admin stats + overview dashboard.

Endpoints
---------
GET  /api/v1/admin/stats/overview
    Platform-wide totals, 24h/7d signup + activity counts, and a 14-day
    signup trend. Excludes the seeded demo organisation.

GET  /api/v1/admin/stats/organizations
    Org list with per-org activity counts (members, spaces, documents,
    conversations, messages) — a richer view than the plain
    ``/admin/organizations`` list, built for the ops dashboard table.

GET  /api/v1/admin/stats/platform-bindings
    The platform-default AI provider bindings (shared DeepSeek, local
    embeddings, ...). These are hidden from every org's own "my models"
    list — this is the only place they're visible, and only to ops.

All three require ``Authorization: Bearer <QUBERI_PLATFORM_ADMIN_TOKEN>``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import require_platform_admin
from quberi_api.services import platform_stats

router = APIRouter(
    prefix="/admin/stats",
    tags=["admin"],
    dependencies=[Depends(require_platform_admin)],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PlatformTotalsView(BaseModel):
    organizations: int
    organizations_personal: int
    organizations_team: int
    users: int
    identities: int
    spaces: int
    documents: int
    documents_indexed: int
    conversations: int
    messages: int


class SignupPointView(BaseModel):
    day: str
    organizations: int
    identities: int


class PlatformOverviewView(BaseModel):
    totals: PlatformTotalsView
    new_organizations_24h: int
    new_organizations_7d: int
    new_identities_24h: int
    new_identities_7d: int
    active_organizations_7d: int
    signups_by_day: list[SignupPointView]


class OrganizationStatsRow(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    tier: str
    status: str
    created_at: datetime
    member_count: int
    space_count: int
    document_count: int
    conversation_count: int
    message_count: int
    owner_email: Optional[str] = None
    is_demo: bool


class OrganizationStatsList(BaseModel):
    items: list[OrganizationStatsRow]
    total: int


class PlatformBindingRowView(BaseModel):
    id: uuid.UUID
    provider_kind: str
    display_name: str
    base_url: Optional[str] = None
    status: str
    models: list[str]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/overview", response_model=PlatformOverviewView)
async def get_overview(
    session: AsyncSession = Depends(get_db_session),
) -> PlatformOverviewView:
    data = await platform_stats.get_overview(session)
    return PlatformOverviewView(
        totals=PlatformTotalsView(**data.totals.__dict__),
        new_organizations_24h=data.new_organizations_24h,
        new_organizations_7d=data.new_organizations_7d,
        new_identities_24h=data.new_identities_24h,
        new_identities_7d=data.new_identities_7d,
        active_organizations_7d=data.active_organizations_7d,
        signups_by_day=[
            SignupPointView(day=p.day, organizations=p.organizations, identities=p.identities)
            for p in data.signups_by_day
        ],
    )


@router.get("/organizations", response_model=OrganizationStatsList)
async def get_organizations(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> OrganizationStatsList:
    rows, total = await platform_stats.list_organizations_detailed(
        session, limit=limit, offset=offset
    )
    return OrganizationStatsList(
        items=[
            OrganizationStatsRow(
                id=r.id,
                name=r.name,
                slug=r.slug,
                tier=r.tier,
                status=r.status,
                created_at=r.created_at,
                member_count=r.member_count,
                space_count=r.space_count,
                document_count=r.document_count,
                conversation_count=r.conversation_count,
                message_count=r.message_count,
                owner_email=r.owner_email,
                is_demo=r.is_demo,
            )
            for r in rows
        ],
        total=total,
    )


@router.get("/platform-bindings", response_model=list[PlatformBindingRowView])
async def get_platform_bindings(
    session: AsyncSession = Depends(get_db_session),
) -> list[PlatformBindingRowView]:
    rows = await platform_stats.list_platform_bindings(session)
    return [
        PlatformBindingRowView(
            id=r.id,
            provider_kind=r.provider_kind,
            display_name=r.display_name,
            base_url=r.base_url,
            status=r.status,
            models=r.models,
        )
        for r in rows
    ]


admin_stats_router = router
