"""Space access-bindings endpoints.

Endpoints
---------
* GET    /api/v1/spaces/{space_id}/access-bindings
* POST   /api/v1/spaces/{space_id}/access-bindings
* DELETE /api/v1/spaces/{space_id}/access-bindings/{binding_id}

Write policy: either the space owner (``Space.owner_type='user'`` AND
``owner_id=caller``) *or* an org owner/admin may grant/revoke access. All
callers must be able to see the space (``get_space_for_user`` gate).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.api.v1.spaces.access_schemas import (
    AccessBindingList,
    AccessBindingView,
    AddAccessBindingRequest,
    EffectiveHitPathView,
    EffectiveMemberList,
    EffectiveMemberView,
)
from quberi_api.services import space_bindings as bindings_service
from quberi_api.services import spaces as spaces_service
from quberi_api.services.authz import (
    caller_has_org_role,
    list_effective_space_members,
)

router = APIRouter(prefix="/spaces", tags=["space-access"])


def _to_response(view: bindings_service.AccessBindingView) -> AccessBindingView:
    return AccessBindingView(
        id=view.binding.id,
        subject_type=view.subject_type,  # type: ignore[arg-type]
        subject_id=view.subject_id,
        subject_display_name=view.subject_display_name,
        subject_subtitle=view.subject_subtitle,
        role_code=view.role_code,  # type: ignore[arg-type]
        created_at=view.binding.created_at,
    )


async def _require_write_access(
    caller: CallerContext,
    session: AsyncSession,
    *,
    space_id: uuid.UUID,
) -> None:
    """Space owner OR org owner/admin may modify access bindings.

    Everyone else gets 403 (the space still resolves via
    ``get_space_for_user`` in the caller path, so we know they at least see
    it — leaking 403 vs 404 for the *write* action is fine).
    """

    # We deliberately fetch the space via the visibility helper so the
    # caller sees a consistent 404 when they can't see the space at all.
    # The manager variant additionally lets org owners/admins reach
    # enterprise-owned spaces they have no binding on.
    view = await spaces_service.get_space_for_manager(
        session,
        space_id=space_id,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
    )
    if view.is_owner:
        await session.commit()
        return

    admin_allowed = await caller_has_org_role(
        session,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
        allowed_codes=("owner", "admin"),
    )
    await session.commit()
    if not admin_allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only the space owner or an organisation owner/admin can modify access",
        )


@router.get(
    "/{space_id}/access-bindings",
    response_model=AccessBindingList,
    summary="List access bindings on a space",
)
async def list_access_bindings(
    space_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AccessBindingList:
    # Any caller who can see the space can inspect its bindings (org
    # owners/admins can also do so for enterprise-owned spaces).
    await spaces_service.get_space_for_manager(
        session,
        space_id=space_id,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
    )
    views = await bindings_service.list_bindings(
        session,
        space_id=space_id,
        organization_id=caller.organization.id,
    )
    items = [_to_response(v) for v in views]
    return AccessBindingList(items=items, total=len(items))


@router.post(
    "/{space_id}/access-bindings",
    status_code=status.HTTP_201_CREATED,
    response_model=AccessBindingView,
    summary="Grant access to a subject (owner or org admin only)",
)
async def add_access_binding(
    space_id: uuid.UUID,
    payload: AddAccessBindingRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AccessBindingView:
    await _require_write_access(caller, session, space_id=space_id)
    view = await bindings_service.add_binding(
        session,
        space_id=space_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
        subject_type=payload.subject_type,
        subject_id=payload.subject_id,
        role_code=payload.role,
    )
    return _to_response(view)


@router.delete(
    "/{space_id}/access-bindings/{binding_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Revoke an access binding (owner or org admin only)",
)
async def delete_access_binding(
    space_id: uuid.UUID,
    binding_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await _require_write_access(caller, session, space_id=space_id)
    await bindings_service.remove_binding(
        session,
        binding_id=binding_id,
        space_id=space_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{space_id}/effective-members",
    response_model=EffectiveMemberList,
    summary="Everyone who effectively has access to a space, with hit paths",
)
async def list_effective_members(
    space_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> EffectiveMemberList:
    """Read-only: any caller who can see the space can inspect who else can.

    Only reveals user id + display name + email + role + hit paths. Does
    not leak any binding row that isn't already visible via
    ``access-bindings``; the hit-path metadata just names the subject that
    granted the access (position / group / unit / user), which is
    intentional — the point is to explain *why* someone has access.
    """

    # Visibility gate — 404s when caller can't see the space at all
    # (org owners/admins may still inspect enterprise-owned spaces).
    await spaces_service.get_space_for_manager(
        session,
        space_id=space_id,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
    )

    views = await list_effective_space_members(
        session,
        organization_id=caller.organization.id,
        space_id=space_id,
    )
    items = [
        EffectiveMemberView(
            user_id=v.user_id,
            display_name=v.display_name,
            email=v.email,
            effective_role=v.effective_role,  # type: ignore[arg-type]
            hit_paths=[
                EffectiveHitPathView(
                    hit_path=p.hit_path,  # type: ignore[arg-type]
                    role_code=p.role_code,  # type: ignore[arg-type]
                    subject_type=p.subject_type,  # type: ignore[arg-type]
                    subject_id=p.subject_id,
                    subject_display_name=p.subject_display_name,
                )
                for p in v.hit_paths
            ],
        )
        for v in views
    ]
    return EffectiveMemberList(items=items, total=len(items))


space_access_router = router
