"""Group endpoints — matrix cohorts crossing the org tree."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.api.v1.organizations.group_schemas import (
    AddGroupMemberRequest,
    CreateGroupRequest,
    EffectiveGroupHitPathView,
    EffectiveGroupMemberList,
    EffectiveGroupMemberView,
    GroupDetail,
    GroupList,
    GroupListItem,
    GroupMemberEntry,
    GroupMemberList,
    GroupMemberType,
    UpdateGroupRequest,
    UserGroupList,
)
from quberi_api.services import groups as groups_service
from quberi_api.services.authz import caller_has_org_role

router = APIRouter(
    prefix="/organizations/current/groups",
    tags=["organization-groups"],
)

_WRITE_ROLES = ("owner", "admin")


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------


def _list_item(view: groups_service.GroupView) -> GroupListItem:
    return GroupListItem.model_validate(view.group).model_copy(
        update={"member_count": view.member_count}
    )


def _detail(view: groups_service.GroupView) -> GroupDetail:
    return GroupDetail.model_validate(view.group).model_copy(
        update={
            "attributes": view.group.attributes,
            "member_count": view.member_count,
        }
    )


def _member(view: groups_service.GroupMemberView) -> GroupMemberEntry:
    return GroupMemberEntry(
        id=view.member.id,
        member_type=view.member_type,  # type: ignore[arg-type]
        member_id=view.member_id,
        display_name=view.display_name,
        subtitle=view.subtitle,
        created_at=view.member.created_at,
    )


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
            detail="only organisation owners or admins can modify groups",
        )


# ---------------------------------------------------------------------------
# Definition endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=GroupList,
    summary="List groups in the caller's organisation",
)
async def list_groups(
    include_archived: bool = False,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> GroupList:
    views = await groups_service.list_groups(
        session,
        organization_id=caller.organization.id,
        include_archived=include_archived,
    )
    items = [_list_item(v) for v in views]
    return GroupList(items=items, total=len(items))


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=GroupDetail,
    summary="Create a group (owner/admin only)",
)
async def create_group(
    payload: CreateGroupRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> GroupDetail:
    await _require_org_admin(caller, session)
    view = await groups_service.create_group(
        session,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
        name=payload.name,
        type=payload.type,
        description=payload.description,
        attributes=payload.attributes,
    )
    return _detail(view)


@router.get(
    "/{group_id}",
    response_model=GroupDetail,
    summary="Fetch a single group",
)
async def get_group(
    group_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> GroupDetail:
    view = await groups_service.get_group(
        session,
        group_id=group_id,
        organization_id=caller.organization.id,
    )
    return _detail(view)


@router.patch(
    "/{group_id}",
    response_model=GroupDetail,
    summary="Update a group (owner/admin only)",
)
async def update_group(
    group_id: uuid.UUID,
    payload: UpdateGroupRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> GroupDetail:
    await _require_org_admin(caller, session)
    view = await groups_service.update_group(
        session,
        group_id=group_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
        name=payload.name,
        type=payload.type,
        description=payload.description,
        attributes=payload.attributes,
    )
    return _detail(view)


@router.post(
    "/{group_id}:archive",
    response_model=GroupDetail,
    summary="Archive a group (owner/admin only)",
)
async def archive_group(
    group_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> GroupDetail:
    await _require_org_admin(caller, session)
    view = await groups_service.archive_group(
        session,
        group_id=group_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
    )
    return _detail(view)


# ---------------------------------------------------------------------------
# Membership endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/{group_id}/members",
    response_model=GroupMemberList,
    summary="List members of a group",
)
async def list_members(
    group_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> GroupMemberList:
    views = await groups_service.list_members(
        session,
        group_id=group_id,
        organization_id=caller.organization.id,
    )
    items = [_member(v) for v in views]
    return GroupMemberList(items=items, total=len(items))


@router.post(
    "/{group_id}/members",
    status_code=status.HTTP_201_CREATED,
    response_model=GroupMemberEntry,
    summary="Add a member to a group (owner/admin only)",
)
async def add_member(
    group_id: uuid.UUID,
    payload: AddGroupMemberRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> GroupMemberEntry:
    await _require_org_admin(caller, session)
    view = await groups_service.add_member(
        session,
        group_id=group_id,
        member_type=payload.member_type,
        member_id=payload.member_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
    )
    return _member(view)


@router.delete(
    "/{group_id}/members/{member_type}/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Remove a member from a group (owner/admin only)",
)
async def remove_member(
    group_id: uuid.UUID,
    member_type: GroupMemberType,
    member_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await _require_org_admin(caller, session)
    await groups_service.remove_member(
        session,
        group_id=group_id,
        member_type=member_type,
        member_id=member_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{group_id}/effective-members",
    response_model=EffectiveGroupMemberList,
    summary="Every real user this group contains, expanded",
)
async def list_effective_members(
    group_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> EffectiveGroupMemberList:
    """Read-only. Any caller in the org may inspect; no write privileges leak."""

    views = await groups_service.list_effective_group_members(
        session,
        group_id=group_id,
        organization_id=caller.organization.id,
    )
    items = [
        EffectiveGroupMemberView(
            user_id=v.user_id,
            display_name=v.display_name,
            email=v.email,
            hit_paths=[
                EffectiveGroupHitPathView(
                    hit_path=p.hit_path,  # type: ignore[arg-type]
                    subject_type=p.subject_type,  # type: ignore[arg-type]
                    subject_id=p.subject_id,
                    subject_display_name=p.subject_display_name,
                )
                for p in v.hit_paths
            ],
        )
        for v in views
    ]
    return EffectiveGroupMemberList(items=items, total=len(items))


# ---------------------------------------------------------------------------
# Reverse lookup: which groups does a specific user belong to?
# ---------------------------------------------------------------------------


@router.get(
    "/for-user/{user_id}",
    response_model=UserGroupList,
    summary="Groups the given user belongs to via any literal path",
)
async def list_groups_for_user(
    user_id: uuid.UUID,
    include_archived: bool = False,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> UserGroupList:
    groups = await groups_service.list_groups_for_user(
        session,
        user_id=user_id,
        organization_id=caller.organization.id,
        include_archived=include_archived,
    )
    # Group returned as-is; the UI wants ``member_count`` so we do a cheap
    # per-group count-fetch. For a single user this is O(N) tiny queries but
    # in practice a user rarely belongs to more than a handful of groups.
    from quberi_api.services.groups import _member_count  # local import: private helper

    items: list[GroupListItem] = []
    for g in groups:
        cnt = await _member_count(session, group_id=g.id)
        items.append(
            GroupListItem.model_validate(g).model_copy(update={"member_count": cnt})
        )
    return UserGroupList(items=items, total=len(items))


groups_router = router
