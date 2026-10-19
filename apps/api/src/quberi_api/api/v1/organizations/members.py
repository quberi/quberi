"""Organisation members endpoints.

* GET   /api/v1/organizations/current/members             — every member of the caller's org
* POST  /api/v1/organizations/current/members             — invite a new member
* PATCH /api/v1/organizations/current/members/{user_id}   — change a member's org role
* POST  /api/v1/organizations/current/members/{user_id}/deactivate   — owner only
* POST  /api/v1/organizations/current/members/{user_id}/reactivate   — owner only

The first three require ``require_caller`` (any authenticated user in the org)
*plus* an ``owner`` or ``admin`` role binding on the organisation itself —
enforced here via ``caller_has_org_role``. The member directory (who's in
the org, their roles) is management information, not something every
regular member needs to see — only owners/admins get it. A full permission
evaluator lands in a later slice; the check-list stays intact, only the
source of truth changes.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.api.v1.organizations._guards import require_org_owner
from quberi_api.api.v1.organizations.schemas import (
    DeactivationEffects,
    DeactivationResponse,
    InviteMemberRequest,
    MemberList,
    MemberSummary,
    ReactivationResponse,
    SetMemberRoleRequest,
)
from quberi_api.services import member_lifecycle
from quberi_api.services import members as members_service
from quberi_api.services.authz import caller_has_org_role
from quberi_api.services.errors import InvalidInputError, NotFoundError

router = APIRouter(prefix="/organizations/current", tags=["organizations"])

# Roles that grant "manage members" on the org today. Extend this list once
# the full permission evaluator lands.
_INVITE_ALLOWED_ROLES = ("owner", "admin")


def _summary(view: members_service.MemberView) -> MemberSummary:
    from quberi_api.api.v1.organizations.schemas import DirectManagerRef

    return MemberSummary(
        id=view.user.id,
        display_name=view.user.display_name,
        email=view.user.email,
        status=view.user.status,
        role_code=view.role_code,
        is_pending=view.is_pending,
        created_at=view.user.created_at,
        updated_at=view.user.updated_at,
        primary_org_unit_id=view.primary_org_unit_id,
        primary_org_unit_name=view.primary_org_unit_name,
        primary_position_id=view.primary_position_id,
        primary_position_name=view.primary_position_name,
        direct_managers=[
            DirectManagerRef(user_id=m.user_id, display_name=m.display_name)
            for m in view.direct_managers
        ],
    )


async def _require_org_admin(
    caller: CallerContext,
    session: AsyncSession,
) -> None:
    allowed = await caller_has_org_role(
        session,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
        allowed_codes=_INVITE_ALLOWED_ROLES,
    )
    # Release the auto-began read transaction so downstream services (which
    # own their own ``session.begin()``) start from a clean slate.
    await session.commit()
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only organisation owners or admins can invite members",
        )


@router.get(
    "/members",
    response_model=MemberList,
    summary="List members of the caller's organisation",
)
async def list_org_members(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> MemberList:
    # Deliberately open to any member, not just owner/admin: the roster is
    # also the "who can I grant access to" picker for space-access and
    # agent-visibility dialogs, which any member who owns a space/agent
    # needs. The *admin console's* Members page is what's gated — that's
    # a frontend nav decision (role_code-driven), not a backend ACL on the
    # roster read itself. Mutations (invite / role change) stay admin-only
    # below.
    views = await members_service.list_members(
        session, organization_id=caller.organization.id
    )
    items = [_summary(view) for view in views]
    return MemberList(items=items, total=len(items))


@router.post(
    "/members",
    status_code=status.HTTP_201_CREATED,
    response_model=MemberSummary,
    summary="Invite a new member",
)
async def invite_org_member(
    payload: InviteMemberRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> MemberSummary:
    await _require_org_admin(caller, session)
    view = await members_service.invite_member(
        session,
        organization_id=caller.organization.id,
        invited_by_user_id=caller.user.id,
        email=str(payload.email),
        display_name=payload.display_name,
        role_code=payload.role,
    )
    return _summary(view)


@router.patch(
    "/members/{user_id}",
    response_model=MemberSummary,
    summary="Change a member's organisation role (owner/admin only; owner role untouchable)",
)
async def set_member_role(
    user_id: uuid.UUID,
    payload: SetMemberRoleRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> MemberSummary:
    await _require_org_admin(caller, session)
    try:
        view = await members_service.set_member_role(
            session,
            organization_id=caller.organization.id,
            actor_user_id=caller.user.id,
            target_user_id=user_id,
            role_code=payload.role,
        )
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except InvalidInputError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return _summary(view)


async def _member_summary(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID
) -> MemberSummary:
    """The member as the list shows them, read after the change was committed."""

    views = await members_service.list_members(session, organization_id=organization_id)
    view = next(v for v in views if v.user.id == user_id)
    return _summary(view)


@router.post(
    "/members/{user_id}/deactivate",
    response_model=DeactivationResponse,
    summary="Deactivate a member (organisation owner only)",
)
async def deactivate_member(
    user_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> DeactivationResponse:
    await require_org_owner(caller, session)
    result = await member_lifecycle.deactivate_member(
        session,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
        target_user_id=user_id,
    )
    member = await _member_summary(
        session, organization_id=caller.organization.id, user_id=user_id
    )
    return DeactivationResponse(
        member=member,
        changed=result.changed,
        effects=DeactivationEffects(**vars(result.effects)),
    )


@router.post(
    "/members/{user_id}/reactivate",
    response_model=ReactivationResponse,
    summary="Reactivate a deactivated member (organisation owner only)",
)
async def reactivate_member(
    user_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ReactivationResponse:
    await require_org_owner(caller, session)
    result = await member_lifecycle.reactivate_member(
        session,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
        target_user_id=user_id,
    )
    member = await _member_summary(
        session, organization_id=caller.organization.id, user_id=user_id
    )
    return ReactivationResponse(
        member=member,
        changed=result.changed,
        personal_spaces_unfrozen=result.personal_spaces_unfrozen,
    )
