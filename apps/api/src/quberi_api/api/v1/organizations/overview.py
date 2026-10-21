"""``GET /organizations/current/overview``: the admin console's landing page numbers.

Organisation owners and admins only. Counts and names, never document text.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.admin_gate import require_org_admin
from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.services import org_overview

_ADMIN_ONLY = "only organisation owners or admins can see the overview"

router = APIRouter(prefix="/organizations/current", tags=["organization-overview"])


class MembersView(BaseModel):
    total: int
    active: int
    invited: int
    suspended: int


class SpacesView(BaseModel):
    total: int
    enterprise: int
    team: int
    personal: int
    archived: int
    frozen: int


class DocumentsView(BaseModel):
    total: int
    indexed: int
    failed: int
    waiting: int


class ExternalView(BaseModel):
    connected_tools: int
    calls_7d: int


class AttentionView(BaseModel):
    code: str
    count: int
    names: list[str] = Field(default_factory=list)


class TrendPointView(BaseModel):
    day: str
    documents: int
    questions: int


class CitedDocumentView(BaseModel):
    document_id: uuid.UUID
    name: str
    space_id: uuid.UUID
    space_name: str
    citations: int
    last_cited_at: datetime


class OverviewView(BaseModel):
    members: MembersView
    spaces: SpacesView
    documents: DocumentsView
    external: ExternalView
    attention: list[AttentionView] = Field(default_factory=list)
    trend: list[TrendPointView] = Field(default_factory=list)
    top_cited: list[CitedDocumentView] = Field(default_factory=list)
    cited_days: int


@router.get(
    "/overview",
    response_model=OverviewView,
    summary="The organisation at a glance (owners and admins only)",
)
async def get_overview(
    tz_offset: int = Query(
        default=0,
        ge=-840,
        le=840,
        description="The viewer's UTC offset in minutes (east is positive), so days line up with theirs.",
    ),
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> OverviewView:
    await require_org_admin(caller, session, detail=_ADMIN_ONLY)
    o = await org_overview.build_overview(
        session,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
        tz_offset_minutes=tz_offset,
    )
    return OverviewView(
        members=MembersView(**vars(o.members)),
        spaces=SpacesView(**vars(o.spaces)),
        documents=DocumentsView(**vars(o.documents)),
        external=ExternalView(**vars(o.external)),
        attention=[
            AttentionView(code=a.code, count=a.count, names=list(a.names)) for a in o.attention
        ],
        trend=[TrendPointView(**vars(p)) for p in o.trend],
        top_cited=[CitedDocumentView(**vars(c)) for c in o.top_cited],
        cited_days=o.cited_days,
    )


overview_router = router
