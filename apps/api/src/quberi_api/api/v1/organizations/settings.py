"""Organisation-level settings — the admin-console "org settings" surface.

Currently three toggles (``allow_byok``, ``allow_external_tools``,
``image_vision_enabled``); more org-wide switches land
here over time without a new router.

* GET   /api/v1/organizations/current/settings — any member may read
* PATCH /api/v1/organizations/current/settings — owner/admin only
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.domain.ai.models import AiModelSelection
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.identity.models import Organization
from quberi_api.services.authz import caller_has_org_role

router = APIRouter(prefix="/organizations/current", tags=["organizations"])

_WRITE_ROLES = ("owner", "admin")


class OrganizationSettingsView(BaseModel):
    allow_byok: bool
    # Outside tools (e.g. DeepSeek Harness) may read this organisation's
    # knowledge through an OAuth grant. Off by default.
    allow_external_tools: bool = False
    # Pictures may be sent to the organisation's chosen vision model to be described so what
    # they show can be searched. Off by default; needs a vision model to be chosen.
    image_vision_enabled: bool = False


class UpdateOrganizationSettingsRequest(BaseModel):
    allow_byok: bool | None = None
    allow_external_tools: bool | None = None
    image_vision_enabled: bool | None = None


async def _require_org_admin(caller: CallerContext, session: AsyncSession) -> None:
    allowed = await caller_has_org_role(
        session,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
        allowed_codes=_WRITE_ROLES,
    )
    await session.commit()
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only organisation owners or admins can change organisation settings",
        )


@router.get("/settings", response_model=OrganizationSettingsView)
async def get_settings(
    caller: CallerContext = Depends(require_caller),
) -> OrganizationSettingsView:
    return OrganizationSettingsView(
        allow_byok=caller.organization.allow_byok,
        allow_external_tools=caller.organization.allow_external_tools,
        image_vision_enabled=caller.organization.image_vision_enabled,
    )


@router.patch("/settings", response_model=OrganizationSettingsView)
async def update_settings(
    payload: UpdateOrganizationSettingsRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> OrganizationSettingsView:
    # ``_require_org_admin`` already commits the read txn it opens, so the
    # write block below starts clean (same pattern as the members/org-units
    # admin guards).
    await _require_org_admin(caller, session)
    async with session.begin():
        org = await session.get(Organization, caller.organization.id)
        assert org is not None  # noqa: S101 — require_caller already resolved it
        if payload.allow_byok is not None:
            org.allow_byok = payload.allow_byok
        if (
            payload.allow_external_tools is not None
            and payload.allow_external_tools != org.allow_external_tools
        ):
            org.allow_external_tools = payload.allow_external_tools
            session.add(
                AuditEvent(
                    organization_id=org.id,
                    actor_type="user",
                    actor_id=caller.user.id,
                    action="organization.external_tools_changed",
                    resource_type="organization",
                    resource_id=org.id,
                    payload={"enabled": payload.allow_external_tools},
                )
            )
        if (
            payload.image_vision_enabled is not None
            and payload.image_vision_enabled != org.image_vision_enabled
        ):
            if payload.image_vision_enabled:
                chosen = (
                    await session.execute(
                        select(AiModelSelection.id).where(
                            AiModelSelection.organization_id == org.id,
                            AiModelSelection.purpose == "vision",
                        )
                    )
                ).first()
                if chosen is None:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="choose a vision model before turning picture descriptions on",
                    )
            org.image_vision_enabled = payload.image_vision_enabled
            # Which model, so the record shows where pictures go; never any content.
            session.add(
                AuditEvent(
                    organization_id=org.id,
                    actor_type="user",
                    actor_id=caller.user.id,
                    action="organization.image_vision_changed",
                    resource_type="organization",
                    resource_id=org.id,
                    payload={"enabled": payload.image_vision_enabled},
                )
            )
        await session.flush()
        allow_byok = org.allow_byok
        allow_external_tools = org.allow_external_tools
        image_vision_enabled = org.image_vision_enabled
    return OrganizationSettingsView(
        allow_byok=allow_byok,
        allow_external_tools=allow_external_tools,
        image_vision_enabled=image_vision_enabled,
    )


organization_settings_router = router
