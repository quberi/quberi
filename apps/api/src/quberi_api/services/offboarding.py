"""What a departing member still holds, and handing their spaces to someone else.

``build_checklist`` answers "what is left to deal with" for one member, before or after
deactivating them. ``transfer_spaces`` hands a batch of their collaboration spaces to a new
owner, one space at a time: each space is its own transaction, a failure on one is reported
and does not undo the others, and repeating a finished request changes nothing.

Personal spaces are never listed and never handed over (they are frozen with the member).
Archived collaboration spaces are listed and handed over like active ones (they stay archived).
Agents are not covered.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.ai.models import AiModelSelection, AiProviderBinding
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.authz.models import Role, RoleBinding
from quberi_api.domain.documents.models import Document, DocumentSource
from quberi_api.domain.identity.models import User
from quberi_api.domain.integrations.models import IntegrationUserToken
from quberi_api.domain.oauth.models import OAuthGrant
from quberi_api.domain.spaces.models import Space
from quberi_api.services import access_guard
from quberi_api.services import members as members_service
from quberi_api.services.authz import list_effective_space_members
from quberi_api.services.errors import (
    ForbiddenError,
    InvalidInputError,
    NotFoundError,
    OffboardingRequestError,
)
from quberi_api.services.space_owners import (
    _owner_role,
    available_owner_ids,
    transfer_owner,
)

_log = logging.getLogger(__name__)

MAX_OWNED_SPACES = 100
MAX_REVIEWER_SPACES = 100
MAX_UPLOADS = 200
MAX_BATCH_TRANSFER = 100  # same as the document review batch limit
_REVIEWER_ROLES = ("owner", "admin")
HANDOVER_SPACE_STATUSES = ("active", "archived")


# ---------------------------------------------------------------------------
# Checklist
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersonRef:
    user_id: uuid.UUID
    display_name: str


@dataclass(frozen=True)
class ChecklistMember:
    id: uuid.UUID
    display_name: str
    email: str
    status: str
    role_code: str


@dataclass(frozen=True)
class OwnedSpaceItem:
    space_id: uuid.UUID
    name: str
    owner_type: str
    status: str  # active | archived
    other_owners: list[PersonRef]
    needs_transfer: bool
    pending_review_count: int


@dataclass(frozen=True)
class ReviewerSpaceItem:
    space_id: uuid.UUID
    name: str
    pending_review_count: int
    other_reviewers: int


@dataclass(frozen=True)
class PendingUploadItem:
    document_id: uuid.UUID
    name: str
    size_bytes: int
    created_at: datetime
    space_id: uuid.UUID
    space_name: str
    reviewer_count: int


@dataclass(frozen=True)
class Impact:
    oauth_grants: int
    feishu_tokens: int
    feishu_sources: int
    private_model_bindings: int
    personal_spaces: int
    org_selections_referencing_private_keys: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Checklist:
    member: ChecklistMember
    personal_space_status: Optional[str]
    owned_spaces: list[OwnedSpaceItem]
    owned_spaces_total: int
    needs_transfer_total: int
    reviewer_spaces: list[ReviewerSpaceItem]
    reviewer_spaces_total: int
    uploads_as_uploader: list[PendingUploadItem]
    uploads_as_uploader_total: int
    impact: Impact


async def _target_member(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID
) -> User:
    user = (
        await session.execute(
            select(User).where(User.id == user_id, User.organization_id == organization_id)
        )
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError("member not found in this organisation")
    if user.status == "archived":
        raise OffboardingRequestError(
            "member_archived", "this member account is archived and cannot be managed", 409
        )
    return user


async def owner_pool(
    session: AsyncSession, *, organization_id: uuid.UUID, space_id: uuid.UUID, member_id: uuid.UUID
) -> set[uuid.UUID]:
    """Owners of the space who could take over, leaving the departing member out.

    While the member is still active they are in ``available_owner_ids`` themselves, so
    they are removed, or a preview before deactivating would always say nobody needs to act.
    """
    available = await available_owner_ids(
        session, space_id=space_id, organization_id=organization_id
    )
    return available - {member_id}


async def effective_reviewer_ids(
    session: AsyncSession, *, organization_id: uuid.UUID, space_id: uuid.UUID
) -> set[uuid.UUID]:
    """People who can approve uploads in the space by any route (group, position, everyone).

    Suspended and archived people are already left out by the member listing; people who
    are only invited cannot sign in yet, so they do not count either.
    """
    views = await list_effective_space_members(
        session, organization_id=organization_id, space_id=space_id
    )
    ids = {v.user_id for v in views if v.effective_role in _REVIEWER_ROLES}
    if not ids:
        return set()
    return set(
        (
            await session.execute(
                select(User.id).where(User.id.in_(ids), User.status == "active")
            )
        )
        .scalars()
        .all()
    )


async def _owned_space_rows(
    session: AsyncSession, *, organization_id: uuid.UUID, member_id: uuid.UUID
) -> list[Space]:
    owner_role = await _owner_role(session)
    bound = select(RoleBinding.resource_id).where(
        RoleBinding.organization_id == organization_id,
        RoleBinding.resource_type == "space",
        RoleBinding.role_id == owner_role.id,
        RoleBinding.subject_type == "user",
        RoleBinding.subject_id == member_id,
    )
    return list(
        (
            await session.execute(
                select(Space).where(
                    Space.organization_id == organization_id,
                    Space.preset != "personal",
                    # An archived space is still readable and needs an owner too.
                    Space.status.in_(HANDOVER_SPACE_STATUSES),
                    (Space.id.in_(bound))
                    | ((Space.owner_type == "user") & (Space.owner_id == member_id)),
                )
            )
        )
        .scalars()
        .all()
    )


async def _pending_counts(
    session: AsyncSession, *, organization_id: uuid.UUID, space_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not space_ids:
        return {}
    rows = (
        await session.execute(
            select(Document.space_id, func.count(Document.id))
            .where(
                Document.organization_id == organization_id,
                Document.space_id.in_(space_ids),
                Document.review_state == "pending",
                Document.status != "archived",
            )
            .group_by(Document.space_id)
        )
    ).all()
    return {space_id: int(count) for space_id, count in rows}


async def _count(session: AsyncSession, stmt) -> int:
    return int((await session.execute(stmt)).scalar_one())


async def build_checklist(
    session: AsyncSession, *, organization_id: uuid.UUID, member_id: uuid.UUID
) -> Checklist:
    """Everything of the member's that someone should look at, capped and with true totals."""

    user = await _target_member(session, organization_id=organization_id, user_id=member_id)
    listed = await members_service.list_members(session, organization_id=organization_id)
    role_code = next((v.role_code for v in listed if v.user.id == member_id), "")

    # -- spaces they own ----------------------------------------------------
    owned = await _owned_space_rows(session, organization_id=organization_id, member_id=member_id)
    pools = {
        s.id: await owner_pool(
            session, organization_id=organization_id, space_id=s.id, member_id=member_id
        )
        for s in owned
    }
    # Spaces that still need a new owner first, so a cut-off never hides one of them.
    ordered = sorted(owned, key=lambda s: (bool(pools[s.id]), s.created_at, str(s.id)))
    needs_total = sum(1 for s in owned if not pools[s.id])
    shown_owned = ordered[:MAX_OWNED_SPACES]
    names = {}
    wanted_people = {uid for s in shown_owned for uid in pools[s.id]}
    if wanted_people:
        names = dict(
            (
                await session.execute(
                    select(User.id, User.display_name).where(User.id.in_(wanted_people))
                )
            ).all()
        )

    # -- spaces where they directly hold the admin (reviewer) role ------------
    admin_role = (
        await session.execute(
            select(Role).where(
                Role.code == "admin", Role.is_system.is_(True), Role.organization_id.is_(None)
            )
        )
    ).scalar_one_or_none()
    reviewer_stmt = None
    reviewer_total = 0
    reviewer_rows: list[Space] = []
    if admin_role is not None:
        reviewer_stmt = (
            select(Space)
            .join(RoleBinding, RoleBinding.resource_id == Space.id)
            .where(
                RoleBinding.organization_id == organization_id,
                RoleBinding.resource_type == "space",
                RoleBinding.role_id == admin_role.id,
                RoleBinding.subject_type == "user",
                RoleBinding.subject_id == member_id,
                Space.preset != "personal",
                Space.status == "active",
            )
        )
        reviewer_total = await _count(
            session, select(func.count()).select_from(reviewer_stmt.subquery())
        )
        reviewer_rows = list(
            (
                await session.execute(
                    reviewer_stmt.order_by(Space.created_at, Space.id).limit(MAX_REVIEWER_SPACES)
                )
            )
            .scalars()
            .all()
        )

    # -- uploads of theirs waiting for review --------------------------------
    upload_filter = [
        Document.organization_id == organization_id,
        Document.uploaded_by_user_id == member_id,
        Document.review_state == "pending",
        Document.status != "archived",
        Space.preset != "personal",
    ]
    uploads_total = await _count(
        session,
        select(func.count(Document.id))
        .join(Space, Space.id == Document.space_id)
        .where(*upload_filter),
    )
    upload_rows = (
        await session.execute(
            select(Document, Space.name)
            .join(Space, Space.id == Document.space_id)
            .where(*upload_filter)
            .order_by(Document.created_at, Document.id)
            .limit(MAX_UPLOADS)
        )
    ).all()

    pending = await _pending_counts(
        session,
        organization_id=organization_id,
        space_ids=list({s.id for s in shown_owned} | {s.id for s in reviewer_rows}),
    )
    reviewers: dict[uuid.UUID, int] = {}

    async def reviewers_other_than_member(space_id: uuid.UUID) -> int:
        if space_id not in reviewers:
            ids = await effective_reviewer_ids(
                session, organization_id=organization_id, space_id=space_id
            )
            reviewers[space_id] = len(ids - {member_id})
        return reviewers[space_id]

    reviewer_items = [
        ReviewerSpaceItem(
            space_id=s.id,
            name=s.name,
            pending_review_count=pending.get(s.id, 0),
            other_reviewers=await reviewers_other_than_member(s.id),
        )
        for s in reviewer_rows
    ]
    upload_items = [
        PendingUploadItem(
            document_id=doc.id,
            name=doc.name,
            size_bytes=doc.size_bytes,
            created_at=doc.created_at,
            space_id=doc.space_id,
            space_name=space_name,
            reviewer_count=await reviewers_other_than_member(doc.space_id),
        )
        for doc, space_name in upload_rows
    ]

    # -- what deactivating will clear ------------------------------------------
    personal = (
        await session.execute(
            select(Space.status).where(
                Space.organization_id == organization_id,
                Space.preset == "personal",
                Space.owner_type == "user",
                Space.owner_id == member_id,
            )
        )
    ).scalars().all()
    private_ids = list(
        (
            await session.execute(
                select(AiProviderBinding.id).where(
                    AiProviderBinding.identity_id == user.identity_id
                )
            )
        )
        .scalars()
        .all()
    )
    purposes: set[str] = set()
    if private_ids:
        found = (
            await session.execute(
                select(AiModelSelection.purpose).where(
                    AiModelSelection.organization_id == organization_id,
                    AiModelSelection.provider_binding_id.in_(private_ids),
                )
            )
        ).scalars()
        # Only chat and embedding can point at a private key; anything else is treated
        # like embedding, the stricter case.
        purposes = {"chat" if p == "chat" else "embedding" for p in found}
    impact = Impact(
        oauth_grants=await _count(
            session,
            select(func.count()).select_from(OAuthGrant).where(
                OAuthGrant.user_id == member_id, OAuthGrant.revoked_at.is_(None)
            ),
        ),
        feishu_tokens=await _count(
            session,
            select(func.count()).select_from(IntegrationUserToken).where(
                IntegrationUserToken.user_id == member_id
            ),
        ),
        feishu_sources=await _count(
            session,
            select(func.count()).select_from(DocumentSource).where(
                DocumentSource.organization_id == organization_id,
                DocumentSource.kind == "feishu",
                DocumentSource.status == "active",
                DocumentSource.created_by_user_id == member_id,
            ),
        ),
        private_model_bindings=len(private_ids),
        personal_spaces=len(personal),
        org_selections_referencing_private_keys=sorted(purposes),
    )
    return Checklist(
        member=ChecklistMember(
            id=user.id,
            display_name=user.display_name,
            email=user.email,
            status=user.status,
            role_code=role_code,
        ),
        personal_space_status=personal[0] if personal else None,
        owned_spaces=[
            OwnedSpaceItem(
                space_id=s.id,
                name=s.name,
                owner_type=s.owner_type,
                status=s.status,
                other_owners=[
                    PersonRef(user_id=uid, display_name=names.get(uid, ""))
                    for uid in sorted(pools[s.id], key=str)
                ],
                needs_transfer=not pools[s.id],
                pending_review_count=pending.get(s.id, 0),
            )
            for s in shown_owned
        ],
        owned_spaces_total=len(owned),
        needs_transfer_total=needs_total,
        reviewer_spaces=reviewer_items,
        reviewer_spaces_total=reviewer_total,
        uploads_as_uploader=upload_items,
        uploads_as_uploader_total=uploads_total,
        impact=impact,
    )


# ---------------------------------------------------------------------------
# Batch handover
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransferItem:
    space_id: uuid.UUID
    status: str  # transferred | already_done | skipped | failed
    code: Optional[str] = None
    pending_review_count: int = 0


@dataclass(frozen=True)
class TransferReport:
    new_owner_user_id: uuid.UUID
    results: list[TransferItem]
    transferred: int
    already_done: int
    skipped: int
    failed: int


async def _validate_request(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    member_id: uuid.UUID,
    new_owner_user_id: uuid.UUID,
    space_ids: list[uuid.UUID],
) -> list[uuid.UUID]:
    """Everything that can refuse the whole request; returns the de-duplicated space ids."""

    member = await _target_member(session, organization_id=organization_id, user_id=member_id)
    if member.status != "suspended":
        raise OffboardingRequestError(
            "member_not_suspended", "deactivate the member before handing over their spaces", 409
        )
    if not space_ids:
        raise OffboardingRequestError("no_spaces", "choose at least one space", 400)
    unique = list(dict.fromkeys(space_ids))
    if len(unique) > MAX_BATCH_TRANSFER:
        raise OffboardingRequestError(
            "batch_too_large", f"at most {MAX_BATCH_TRANSFER} spaces at a time", 400
        )
    new_owner = (
        await session.execute(
            select(User).where(
                User.id == new_owner_user_id, User.organization_id == organization_id
            )
        )
    ).scalar_one_or_none()
    if new_owner is None:
        raise OffboardingRequestError(
            "new_owner_not_found", "the new owner is not a member of this organisation", 400
        )
    if new_owner.id == member_id:
        raise OffboardingRequestError(
            "new_owner_is_departed_member", "the new owner cannot be the departed member", 400
        )
    if new_owner.status != "active":
        raise OffboardingRequestError(
            "new_owner_not_active", "the new owner must be an active member", 400
        )
    if await access_guard.user_is_restricted_owner(
        session, organization_id=organization_id, user_id=new_owner.id
    ):
        raise OffboardingRequestError(
            "new_owner_is_org_owner", "the organisation owner cannot own a space", 400
        )
    return unique


async def _holds_ownership(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    space: Space,
    member_id: uuid.UUID,
) -> bool:
    if space.owner_type == "user" and space.owner_id == member_id:
        return True
    owner_role = await _owner_role(session)
    return (
        await _count(
            session,
            select(func.count()).select_from(RoleBinding).where(
                RoleBinding.organization_id == organization_id,
                RoleBinding.resource_type == "space",
                RoleBinding.resource_id == space.id,
                RoleBinding.role_id == owner_role.id,
                RoleBinding.subject_type == "user",
                RoleBinding.subject_id == member_id,
            ),
        )
        > 0
    )


async def _handed_over_before(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    member_id: uuid.UUID,
    new_owner_user_id: uuid.UUID,
) -> bool:
    return (
        await _count(
            session,
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.organization_id == organization_id,
                AuditEvent.resource_type == "space",
                AuditEvent.resource_id == space_id,
                AuditEvent.action == "space.owner_transferred",
                AuditEvent.payload["offboarded_user_id"].astext == str(member_id),
                AuditEvent.payload["new_owner_user_id"].astext == str(new_owner_user_id),
            ),
        )
        > 0
    )


async def transfer_spaces(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    member_id: uuid.UUID,
    new_owner_user_id: uuid.UUID,
    space_ids: list[uuid.UUID],
) -> TransferReport:
    """Hand the member's collaboration spaces to ``new_owner_user_id``, reporting each one."""

    if not await access_guard.is_org_owner(
        session, organization_id=organization_id, user_id=actor_user_id
    ):
        raise ForbiddenError("only the organisation owner can hand over a member's spaces")
    unique = await _validate_request(
        session,
        organization_id=organization_id,
        member_id=member_id,
        new_owner_user_id=new_owner_user_id,
        space_ids=space_ids,
    )
    await session.rollback()  # the checks above only read

    results: list[TransferItem] = []
    for space_id in unique:
        results.append(
            await _transfer_one(
                session,
                organization_id=organization_id,
                actor_user_id=actor_user_id,
                member_id=member_id,
                new_owner_user_id=new_owner_user_id,
                space_id=space_id,
            )
        )

    report = TransferReport(
        new_owner_user_id=new_owner_user_id,
        results=results,
        transferred=sum(r.status == "transferred" for r in results),
        already_done=sum(r.status == "already_done" for r in results),
        skipped=sum(r.status == "skipped" for r in results),
        failed=sum(r.status == "failed" for r in results),
    )
    if report.transferred:
        async with session.begin():
            session.add(
                AuditEvent(
                    organization_id=organization_id,
                    actor_type="user",
                    actor_id=actor_user_id,
                    action="member.spaces_transferred",
                    resource_type="user",
                    resource_id=member_id,
                    payload={
                        "new_owner_user_id": str(new_owner_user_id),
                        "requested": len(unique),
                        "transferred": report.transferred,
                        "already_done": report.already_done,
                        "skipped": report.skipped,
                        "failed": report.failed,
                    },
                )
            )
    return report


async def _transfer_one(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    member_id: uuid.UUID,
    new_owner_user_id: uuid.UUID,
    space_id: uuid.UUID,
) -> TransferItem:
    try:
        decision = await _decide(
            session,
            organization_id=organization_id,
            member_id=member_id,
            new_owner_user_id=new_owner_user_id,
            space_id=space_id,
        )
        # The reads above opened a transaction; the handover opens its own.
        await session.rollback()
        if isinstance(decision, TransferItem):
            return decision
        await transfer_owner(
            session,
            space_id=space_id,
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            new_owner_user_id=new_owner_user_id,
            offboarded_user_id=member_id,
            allow_archived=True,
        )
        return TransferItem(space_id, "transferred", None, decision)
    except ForbiddenError:
        return TransferItem(space_id, "failed", "not_allowed")
    except NotFoundError:
        return TransferItem(space_id, "failed", "space_not_found")
    except InvalidInputError:
        return TransferItem(space_id, "failed", "invalid_state")
    except Exception:  # noqa: BLE001 - one space must not stop the rest
        _log.exception("handover of space %s failed", space_id)
        return TransferItem(space_id, "failed", "invalid_state")
    finally:
        if session.in_transaction():
            await session.rollback()


async def _decide(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    member_id: uuid.UUID,
    new_owner_user_id: uuid.UUID,
    space_id: uuid.UUID,
) -> TransferItem | int:
    """Either the final answer for a space that needs no handover, or its pending-review count."""

    space = (
        await session.execute(
            select(Space).where(Space.id == space_id, Space.organization_id == organization_id)
        )
    ).scalar_one_or_none()
    if space is None:
        return TransferItem(space_id, "failed", "space_not_found")
    if space.preset == "personal":
        return TransferItem(space_id, "skipped", "personal_space")
    holds = await _holds_ownership(
        session, organization_id=organization_id, space=space, member_id=member_id
    )
    if not holds:
        if await _handed_over_before(
            session,
            organization_id=organization_id,
            space_id=space_id,
            member_id=member_id,
            new_owner_user_id=new_owner_user_id,
        ):
            return TransferItem(space_id, "already_done")
        return TransferItem(space_id, "skipped", "not_owned_by_member")
    if space.status not in HANDOVER_SPACE_STATUSES:
        return TransferItem(space_id, "skipped", "space_not_active")
    if await owner_pool(
        session, organization_id=organization_id, space_id=space_id, member_id=member_id
    ):
        return TransferItem(space_id, "skipped", "has_other_owner")
    pending = await _pending_counts(session, organization_id=organization_id, space_ids=[space_id])
    return pending.get(space_id, 0)
