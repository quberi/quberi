"""A person's list of outside tools connected to their account, and revoking them.

* GET    /api/v1/me/external-access        - every tool the signed-in person has connected
* DELETE /api/v1/me/external-access/{id}   - disconnect one (works at once)

Grants are listed across all the person's organisations, because the person
(not the organisation) is who authorised the tool.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.domain.identity.models import Organization, User
from quberi_api.domain.oauth.models import OAuthGrant
from quberi_api.services.oauth import core

router = APIRouter(prefix="/me/external-access", tags=["external"])


class ExternalGrantView(BaseModel):
    id: uuid.UUID
    client_name: str
    organization_id: uuid.UUID
    organization_name: str
    scope: str
    created_at: datetime
    last_used_at: Optional[datetime]
    expires_at: datetime


class ExternalGrantList(BaseModel):
    items: list[ExternalGrantView]


@router.get("", response_model=ExternalGrantList)
async def list_grants(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ExternalGrantList:
    if caller.is_guest:
        return ExternalGrantList(items=[])
    rows = (
        await session.execute(
            select(OAuthGrant, Organization)
            .join(User, User.id == OAuthGrant.user_id)
            .join(Organization, Organization.id == OAuthGrant.organization_id)
            .where(
                User.identity_id == caller.user.identity_id,
                OAuthGrant.revoked_at.is_(None),
            )
            .order_by(OAuthGrant.created_at.desc())
        )
    ).all()
    await session.commit()
    return ExternalGrantList(
        items=[
            ExternalGrantView(
                id=g.id,
                client_name=g.client_name,
                organization_id=g.organization_id,
                organization_name=o.name,
                scope=g.scope,
                created_at=g.created_at,
                last_used_at=g.last_used_at,
                expires_at=min(g.refresh_expires_at, g.absolute_expires_at),
            )
            for g, o in rows
        ]
    )


@router.delete("/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_grant(
    grant_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    if caller.is_guest:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="sign in first")
    ok = await core.revoke_grant_for_user(
        session, grant_id=grant_id, identity_id=caller.user.identity_id
    )
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


me_external_access_router = router
