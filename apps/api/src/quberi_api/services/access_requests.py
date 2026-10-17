"""Asking a space's owner for access, and the owner's answer.

An organisation administrator can manage an enterprise space without being able to read it,
and nobody can give themselves access (``services.access_guard``). The way in is to ask:

1. The administrator names the space, the role they want (viewer or editor) and why.
2. An owner of the space who can act sees it in their pending list and approves or rejects.
3. Approving grants that role. The owner is the one acting, so the rule against giving
   yourself access is not in the way. Rejecting records the owner's note.

Only organisation owners and admins can ask, and only for enterprise (organisation-owned)
spaces; ordinary members never see those spaces exist. Every step is audited. If a space has
no owner who can act, there is nobody to ask: the organisation owner names a new owner first
(``services.space_owners``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.authz.models import RoleBinding
from quberi_api.domain.enums import SPACE_ACCESS_REQUEST_ROLES
from quberi_api.domain.identity.models import User
from quberi_api.domain.spaces.models import Space, SpaceAccessRequest
from quberi_api.services import space_bindings, space_owners
from quberi_api.services.authz import caller_has_org_role
from quberi_api.services.errors import (
    AlreadyExistsError,
    ForbiddenError,
    InvalidInputError,
    NotFoundError,
)

MAX_REASON_CHARS = 500
MAX_NOTE_CHARS = 500
_ROLE_STRENGTH = {"viewer": 1, "editor": 2}


@dataclass(frozen=True)
class RequestView:
    request: SpaceAccessRequest
    space_name: str
    requester_name: str
    requester_email: str
    decided_by_name: Optional[str]


async def _view(session: AsyncSession, request: SpaceAccessRequest) -> RequestView:
    space_name = (
        await session.execute(select(Space.name).where(Space.id == request.space_id))
    ).scalar_one_or_none() or ""
    requester = (
        await session.execute(
            select(User.display_name, User.email).where(User.id == request.requester_user_id)
        )
    ).one_or_none()
    decider = None
    if request.decided_by_user_id is not None:
        decider = (
            await session.execute(
                select(User.display_name).where(User.id == request.decided_by_user_id)
            )
        ).scalar_one_or_none()
    return RequestView(
        request=request,
        space_name=space_name,
        requester_name=requester[0] if requester else "",
        requester_email=requester[1] if requester else "",
        decided_by_name=decider,
    )


async def _current_role(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    space_id: uuid.UUID,
) -> Optional[str]:
    from quberi_api.services.documents import core as documents_core

    return await documents_core._caller_space_role(
        session, user_id=user_id, organization_id=organization_id, space_id=space_id
    )


async def create_request(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    requester_user_id: uuid.UUID,
    space_id: uuid.UUID,
    role_code: str,
    reason: str,
) -> RequestView:
    if role_code not in SPACE_ACCESS_REQUEST_ROLES:
        raise InvalidInputError(f"role must be one of {list(SPACE_ACCESS_REQUEST_ROLES)}")
    reason = (reason or "").strip()
    if not reason:
        raise InvalidInputError("say why you need access")
    if len(reason) > MAX_REASON_CHARS:
        raise InvalidInputError(f"the reason is longer than {MAX_REASON_CHARS} characters")

    is_admin = await caller_has_org_role(
        session,
        organization_id=organization_id,
        user_id=requester_user_id,
        allowed_codes=("owner", "admin"),
    )
    if not is_admin:
        raise ForbiddenError("only organisation owners and admins can ask for access to a space")
    space = (
        await session.execute(
            select(Space).where(Space.id == space_id, Space.organization_id == organization_id)
        )
    ).scalar_one_or_none()
    if space is None or space.owner_type != "organization" or space.status != "active":
        raise NotFoundError(f"space not found: {space_id}")

    current = await _current_role(
        session, organization_id=organization_id, user_id=requester_user_id, space_id=space_id
    )
    if current is not None and _ROLE_STRENGTH.get(current, 99) >= _ROLE_STRENGTH[role_code]:
        raise InvalidInputError("you already have that access to this space")
    if not await space_owners.available_owner_ids(
        session, space_id=space_id, organization_id=organization_id
    ):
        raise InvalidInputError(
            "this space has no owner who can approve; the organisation owner can name a new one first"
        )
    await session.commit()
    async with session.begin():
        request = SpaceAccessRequest(
            organization_id=organization_id,
            space_id=space_id,
            requester_user_id=requester_user_id,
            role_code=role_code,
            reason=reason,
            status="pending",
        )
        session.add(request)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise AlreadyExistsError("you already have a pending request for this space") from exc
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=requester_user_id,
                action="space.access_requested",
                resource_type="space",
                resource_id=space_id,
                space_id=space_id,
                payload={"request_id": str(request.id), "role": role_code, "reason": reason},
            )
        )
        await session.flush()
        await session.refresh(request, attribute_names=["created_at", "updated_at"])
    return await _view(session, request)


async def list_to_decide(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID
) -> list[RequestView]:
    """Pending requests for spaces this person can approve (they own it and can act)."""

    role = await space_owners._owner_role(session)
    owned = (
        (
            await session.execute(
                select(RoleBinding.resource_id).where(
                    RoleBinding.organization_id == organization_id,
                    RoleBinding.resource_type == "space",
                    RoleBinding.role_id == role.id,
                    RoleBinding.subject_type == "user",
                    RoleBinding.subject_id == user_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not owned:
        return []
    deciders = [
        sid
        for sid in owned
        if user_id
        in await space_owners.available_owner_ids(
            session, space_id=sid, organization_id=organization_id
        )
    ]
    if not deciders:
        return []
    rows = (
        (
            await session.execute(
                select(SpaceAccessRequest)
                .where(
                    SpaceAccessRequest.organization_id == organization_id,
                    SpaceAccessRequest.space_id.in_(deciders),
                    SpaceAccessRequest.status == "pending",
                    SpaceAccessRequest.requester_user_id != user_id,
                )
                .order_by(SpaceAccessRequest.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return [await _view(session, r) for r in rows]


async def list_mine(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID, limit: int = 50
) -> list[RequestView]:
    rows = (
        (
            await session.execute(
                select(SpaceAccessRequest)
                .where(
                    SpaceAccessRequest.organization_id == organization_id,
                    SpaceAccessRequest.requester_user_id == user_id,
                )
                .order_by(SpaceAccessRequest.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [await _view(session, r) for r in rows]


async def pending_space_ids_for(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID
) -> dict[uuid.UUID, uuid.UUID]:
    """space id -> pending request id, for the spaces this person has asked about."""

    rows = (
        await session.execute(
            select(SpaceAccessRequest.space_id, SpaceAccessRequest.id).where(
                SpaceAccessRequest.organization_id == organization_id,
                SpaceAccessRequest.requester_user_id == user_id,
                SpaceAccessRequest.status == "pending",
            )
        )
    ).all()
    return {space_id: request_id for space_id, request_id in rows}


async def _load_pending(
    session: AsyncSession, *, organization_id: uuid.UUID, request_id: uuid.UUID
) -> SpaceAccessRequest:
    request = (
        await session.execute(
            select(SpaceAccessRequest).where(
                SpaceAccessRequest.id == request_id,
                SpaceAccessRequest.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if request is None:
        raise NotFoundError(f"request not found: {request_id}")
    if request.status != "pending":
        raise InvalidInputError(f"this request was already {request.status}")
    return request


async def decide(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    request_id: uuid.UUID,
    approve: bool,
    note: str = "",
) -> RequestView:
    note = (note or "").strip()
    if len(note) > MAX_NOTE_CHARS:
        raise InvalidInputError(f"the note is longer than {MAX_NOTE_CHARS} characters")
    async with session.begin():
        request = await _load_pending(
            session, organization_id=organization_id, request_id=request_id
        )
        if request.requester_user_id == actor_user_id:
            raise ForbiddenError("you cannot decide your own request")
        available = await space_owners.available_owner_ids(
            session, space_id=request.space_id, organization_id=organization_id
        )
        if actor_user_id not in available:
            # Not an owner who can act. Say nothing about the request to anyone else.
            raise NotFoundError(f"request not found: {request_id}")
        if approve:
            space = (
                await session.execute(select(Space).where(Space.id == request.space_id))
            ).scalar_one()
            try:
                await space_bindings.grant_in_transaction(
                    session,
                    space=space,
                    organization_id=organization_id,
                    actor_user_id=actor_user_id,
                    subject_type="user",
                    subject_id=request.requester_user_id,
                    role_code=request.role_code,
                )
            except AlreadyExistsError:
                pass  # they were given that role meanwhile; the request is moot
        request.status = "approved" if approve else "rejected"
        request.decided_by_user_id = actor_user_id
        request.decided_at = datetime.now(timezone.utc)
        request.decision_note = note or None
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="space.access_request_approved" if approve else "space.access_request_rejected",
                resource_type="space",
                resource_id=request.space_id,
                space_id=request.space_id,
                payload={
                    "request_id": str(request.id),
                    "requester_user_id": str(request.requester_user_id),
                    "role": request.role_code,
                    "note": note,
                },
            )
        )
        await session.flush()
        await session.refresh(request, attribute_names=["updated_at"])
    return await _view(session, request)


async def cancel(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    request_id: uuid.UUID,
) -> RequestView:
    async with session.begin():
        request = await _load_pending(
            session, organization_id=organization_id, request_id=request_id
        )
        if request.requester_user_id != actor_user_id:
            raise NotFoundError(f"request not found: {request_id}")
        request.status = "cancelled"
        request.decided_at = datetime.now(timezone.utc)
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="space.access_request_cancelled",
                resource_type="space",
                resource_id=request.space_id,
                space_id=request.space_id,
                payload={"request_id": str(request.id)},
            )
        )
        await session.flush()
        await session.refresh(request, attribute_names=["updated_at"])
    return await _view(session, request)

