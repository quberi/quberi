"""Organisation-unit endpoints (self-managed org tree).

Reads are open to any authenticated caller in the tenant; writes are gated
to org-level owner/admin via the same ``caller_has_org_role`` helper used
by the invite flow. Full permission evaluation lands in a later slice; the
check-list stays the same, only the source of truth changes.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.api.v1.organizations.org_unit_schemas import (
    AssignUserRequest,
    CreateOrgUnitRequest,
    MoveOrgUnitRequest,
    OrgUnitDetail,
    OrgUnitMember,
    OrgUnitMemberList,
    OrgUnitTree,
    OrgUnitTreeItem,
    UpdateOrgUnitRequest,
)
from quberi_api.services import org_units as org_units_service
from quberi_api.services.authz import caller_has_org_role

router = APIRouter(
    prefix="/organizations/current/org-units",
    tags=["organization-units"],
)


_WRITE_ROLES = ("owner", "admin")


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------


def _tree_item(view: org_units_service.OrgUnitView) -> OrgUnitTreeItem:
    return OrgUnitTreeItem.model_validate(view.unit).model_copy(
        update={"ancestor_ids": list(view.ancestor_ids)}
    )


def _detail(view: org_units_service.OrgUnitView) -> OrgUnitDetail:
    return OrgUnitDetail.model_validate(view.unit).model_copy(
        update={"ancestor_ids": list(view.ancestor_ids)}
    )


def _member(view: org_units_service.OrgUnitMemberView) -> OrgUnitMember:
    return OrgUnitMember(
        membership_id=view.membership_id,
        user_id=view.user.id,
        display_name=view.user.display_name,
        email=view.user.email,
        status=view.user.status,
        is_primary=view.is_primary,
    )


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


async def _require_org_admin(caller: CallerContext, session: AsyncSession) -> None:
    allowed = await caller_has_org_role(
        session,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
        allowed_codes=_WRITE_ROLES,
    )
    # Release the read transaction so downstream service ``session.begin()``
    # blocks start cleanly (same pattern as members router).
    await session.commit()
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "only organisation owners or admins can modify the organisation tree"
            ),
        )


# ---------------------------------------------------------------------------
# Tree endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=OrgUnitTree,
    summary="List every org unit in the caller's organisation",
)
async def list_units(
    include_archived: bool = False,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> OrgUnitTree:
    # Open to any member — also used as the "assign to this unit" picker
    # in access/agent-visibility dialogs. Writes stay admin-only below.
    views = await org_units_service.list_tree(
        session,
        organization_id=caller.organization.id,
        include_archived=include_archived,
    )
    items = [_tree_item(v) for v in views]
    return OrgUnitTree(items=items, total=len(items))


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=OrgUnitDetail,
    summary="Create an org unit (owner/admin only)",
)
async def create_unit(
    payload: CreateOrgUnitRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> OrgUnitDetail:
    await _require_org_admin(caller, session)
    view = await org_units_service.create_org_unit(
        session,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
        name=payload.name,
        type=payload.type,
        parent_id=payload.parent_id,
        code=payload.code,
        manager_user_id=payload.manager_user_id,
        attributes=payload.attributes,
    )
    return _detail(view)


@router.get(
    "/{unit_id}",
    response_model=OrgUnitDetail,
    summary="Fetch a single org unit",
)
async def get_unit(
    unit_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> OrgUnitDetail:
    view = await org_units_service.get_org_unit(
        session,
        unit_id=unit_id,
        organization_id=caller.organization.id,
    )
    return _detail(view)


@router.patch(
    "/{unit_id}",
    response_model=OrgUnitDetail,
    summary="Update an org unit (owner/admin only)",
)
async def update_unit(
    unit_id: uuid.UUID,
    payload: UpdateOrgUnitRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> OrgUnitDetail:
    await _require_org_admin(caller, session)
    view = await org_units_service.update_org_unit(
        session,
        unit_id=unit_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
        name=payload.name,
        type=payload.type,
        code=payload.code,
        manager_user_id=payload.manager_user_id,
        clear_manager=payload.clear_manager,
        attributes=payload.attributes,
    )
    return _detail(view)


@router.post(
    "/{unit_id}:move",
    response_model=OrgUnitDetail,
    summary="Reparent an org unit (owner/admin only)",
)
async def move_unit(
    unit_id: uuid.UUID,
    payload: MoveOrgUnitRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> OrgUnitDetail:
    await _require_org_admin(caller, session)
    view = await org_units_service.move_org_unit(
        session,
        unit_id=unit_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
        new_parent_id=payload.new_parent_id,
    )
    return _detail(view)


@router.post(
    "/{unit_id}:archive",
    response_model=OrgUnitDetail,
    summary="Archive an org unit (owner/admin only)",
)
async def archive_unit(
    unit_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> OrgUnitDetail:
    await _require_org_admin(caller, session)
    view = await org_units_service.archive_org_unit(
        session,
        unit_id=unit_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
    )
    return _detail(view)


# ---------------------------------------------------------------------------
# Membership endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/{unit_id}/members",
    response_model=OrgUnitMemberList,
    summary="List users assigned to an org unit",
)
async def list_unit_members(
    unit_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> OrgUnitMemberList:
    views = await org_units_service.list_members_of_unit(
        session,
        unit_id=unit_id,
        organization_id=caller.organization.id,
    )
    items = [_member(v) for v in views]
    return OrgUnitMemberList(items=items, total=len(items))


@router.post(
    "/{unit_id}/members",
    status_code=status.HTTP_201_CREATED,
    response_model=OrgUnitMember,
    summary="Assign a user to an org unit (owner/admin only)",
)
async def add_unit_member(
    unit_id: uuid.UUID,
    payload: AssignUserRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> OrgUnitMember:
    await _require_org_admin(caller, session)
    view = await org_units_service.assign_user_to_unit(
        session,
        unit_id=unit_id,
        user_id=payload.user_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
        make_primary=payload.make_primary,
    )
    return _member(view)


@router.delete(
    "/{unit_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Remove a user from an org unit (owner/admin only)",
)
async def remove_unit_member(
    unit_id: uuid.UUID,
    user_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await _require_org_admin(caller, session)
    await org_units_service.remove_user_from_unit(
        session,
        unit_id=unit_id,
        user_id=user_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{unit_id}/members/{user_id}:set-primary",
    response_model=OrgUnitMember,
    summary="Mark this org unit as the user's primary (owner/admin only)",
)
async def set_unit_primary(
    unit_id: uuid.UUID,
    user_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> OrgUnitMember:
    await _require_org_admin(caller, session)
    view = await org_units_service.set_primary_unit(
        session,
        user_id=user_id,
        unit_id=unit_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
    )
    return _member(view)


# Re-export as ``org_units_router`` for the v1 router.
org_units_router = router
