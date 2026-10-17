"""Two rules about who may hold power over knowledge, for team and enterprise organisations.

1. **Nobody gives themselves access.** A change that makes the person who made it able to
   read something they could not read before is refused: adding themselves to a group, a
   unit or a position that has access, granting a space role to themselves or to a group
   they are in, sharing a document with themselves, lifting a denial that applies to them,
   or naming themselves the new owner of a space. It does not matter how indirect the path
   is, because the check compares what the person can read before and after the change
   rather than listing forbidden operations. A space owner is not blocked on their own space:
   they can already read it. Access always comes from someone else, normally the space owner.

2. **The organisation owner is the super administrator, not a space administrator.** In a
   team or enterprise organisation the owner cannot create spaces and cannot hold the owner
   or admin role on one (directly, or through a group, unit or position). They can still be
   made a viewer or editor by a space owner, and they keep their own personal space.

Personal organisations have one person who is both the owner and the user, so neither rule
applies there.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.documents.models import Document
from quberi_api.domain.identity.models import Organization
from quberi_api.services import doc_acl
from quberi_api.services.authz import caller_has_org_role, list_accessible_space_ids
from quberi_api.services.errors import ForbiddenError

SELF_GRANT_MESSAGE = (
    "You cannot give yourself access. Ask the owner of the space to grant it, or to approve "
    "your request."
)
OWNER_CANNOT_MANAGE_SPACES_MESSAGE = (
    "The organisation owner cannot create spaces or be an owner or admin of one. "
    "Use an admin account for that."
)


async def is_restricted_org(session: AsyncSession, *, organization_id: uuid.UUID) -> bool:
    """True for team and enterprise organisations, where the rules above apply."""

    tier = (
        await session.execute(select(Organization.tier).where(Organization.id == organization_id))
    ).scalar_one_or_none()
    return tier is not None and tier != "personal"


async def is_org_owner(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    return await caller_has_org_role(
        session, organization_id=organization_id, user_id=user_id, allowed_codes=("owner",)
    )


async def user_is_restricted_owner(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """The organisation owner of a team or enterprise organisation."""

    return await is_restricted_org(session, organization_id=organization_id) and await is_org_owner(
        session, organization_id=organization_id, user_id=user_id
    )


async def forbid_owner_creating_spaces(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    if await user_is_restricted_owner(session, organization_id=organization_id, user_id=user_id):
        raise ForbiddenError(OWNER_CANNOT_MANAGE_SPACES_MESSAGE)


@dataclass
class SelfGrantGuard:
    """Remembers what a person could read before a change, and checks again after it."""

    session: AsyncSession
    user_id: uuid.UUID
    organization_id: uuid.UUID
    active: bool
    spaces: frozenset[uuid.UUID] = frozenset()
    granted: frozenset[uuid.UUID] = frozenset()
    denied: frozenset[uuid.UUID] = frozenset()

    async def check(self) -> None:
        """Raise ``ForbiddenError`` if the change gave this person access. Flush first."""

        if not self.active:
            return
        spaces = frozenset(
            await list_accessible_space_ids(
                self.session, user_id=self.user_id, organization_id=self.organization_id
            )
        )
        access = await doc_acl.load_doc_access(
            self.session, user_id=self.user_id, organization_id=self.organization_id
        )
        if spaces - self.spaces:
            raise ForbiddenError(SELF_GRANT_MESSAGE)
        # Documents newly shared with them, or a denial against them lifted. A share inside a
        # space they already read as a whole adds nothing; a lifted denial is allowed to the
        # owner of that space.
        shared = {d for d in access.granted if d not in self.granted}
        lifted = self.denied - access.denied
        suspect = {d for d in shared if access.granted[d] not in spaces} | lifted
        if suspect:
            from quberi_api.services.documents import core as documents_core

            rows = (
                await self.session.execute(
                    select(Document.id, Document.space_id).where(Document.id.in_(suspect))
                )
            ).all()
            for doc_id, space_id in rows:
                role = await documents_core._caller_space_role(
                    self.session,
                    user_id=self.user_id,
                    organization_id=self.organization_id,
                    space_id=space_id,
                )
                if role == "owner":
                    continue  # the owner of the space may share or unhide inside it
                raise ForbiddenError(SELF_GRANT_MESSAGE)


async def watch(
    session: AsyncSession, *, actor_user_id: uuid.UUID, organization_id: uuid.UUID
) -> SelfGrantGuard:
    """Take the 'before' picture. Call inside the transaction, before changing anything."""

    if not await is_restricted_org(session, organization_id=organization_id):
        return SelfGrantGuard(session, actor_user_id, organization_id, active=False)
    spaces = frozenset(
        await list_accessible_space_ids(
            session, user_id=actor_user_id, organization_id=organization_id
        )
    )
    access = await doc_acl.load_doc_access(
        session, user_id=actor_user_id, organization_id=organization_id
    )
    return SelfGrantGuard(
        session,
        actor_user_id,
        organization_id,
        active=True,
        spaces=spaces,
        granted=frozenset(access.granted),
        denied=access.denied,
    )
