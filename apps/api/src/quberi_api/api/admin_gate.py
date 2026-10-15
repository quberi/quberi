"""Shared "organisation owner or admin only" check for REST handlers."""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.security import CallerContext
from quberi_api.services.authz import caller_has_org_role


async def require_org_admin(
    caller: CallerContext,
    session: AsyncSession,
    *,
    detail: str = "only organisation owners or admins can do this",
) -> None:
    """403 unless the caller holds the owner or admin role on their organisation."""

    ok = await caller_has_org_role(
        session,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
        allowed_codes=("owner", "admin"),
    )
    # The role lookup auto-began a read transaction; close it so the handler
    # can open its own write transaction.
    await session.commit()
    if not ok:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
