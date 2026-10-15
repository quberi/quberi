"""Free-quota status endpoint.

Lets the client show "N free messages left this month" and switch to a
"add your own key" prompt once the allowance is spent. Read-only.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.services import usage as usage_service

router = APIRouter(prefix="/usage", tags=["usage"])


class UsageStatusResponse(BaseModel):
    # metered=False → BYOK (no limit). limit=0 → unlimited tier.
    metered: bool
    limit: int
    used: int
    remaining: int  # -1 when unlimited
    is_guest: bool


@router.get("", response_model=UsageStatusResponse, summary="Free-quota status")
async def get_usage(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> UsageStatusResponse:
    status = await usage_service.get_status(
        session,
        organization_id=caller.organization.id,
        tier=caller.organization.tier,
    )
    return UsageStatusResponse(
        metered=status.metered,
        limit=status.limit,
        used=status.used,
        remaining=status.remaining,
        is_guest=caller.is_guest,
    )


usage_router = router
