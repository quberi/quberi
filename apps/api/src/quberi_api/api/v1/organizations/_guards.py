"""Shared gates for the organisation endpoints."""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.security import CallerContext
from quberi_api.services.access_guard import is_org_owner
from quberi_api.services.member_lifecycle import NOT_OWNER_MESSAGE


async def require_org_owner(caller: CallerContext, session: AsyncSession) -> None:
    """Only the organisation owner passes; everyone else (admins included) gets 403."""

    allowed = await is_org_owner(
        session, organization_id=caller.organization.id, user_id=caller.user.id
    )
    # Release the read transaction so the service can open its own.
    await session.commit()
    if not allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=NOT_OWNER_MESSAGE)
