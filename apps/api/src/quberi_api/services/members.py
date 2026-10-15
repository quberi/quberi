"""Member management: list and invite users into an organisation.

Scope for this slice
--------------------
* ``list_members`` returns every ``User`` in an organisation along with
  their org-level role code (empty string if none).
* ``invite_member`` creates (or reuses) an ``Identity`` for the given email,
  creates a ``User(status='invited')`` in the target org, and grants the
  chosen system role scoped to that organisation. No email is actually sent
  today; the returned ``MemberView`` is enough for the UI to render the
  pending row and (later) surface a copyable invite link.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.authz.catalog import SYSTEM_ROLE_CODES
from quberi_api.domain.authz.models import Role, RoleBinding
from quberi_api.domain.identity.models import Identity, User
from quberi_api.domain.org.models import OrganizationUnit, UserOrganizationUnit
from quberi_api.domain.positions.models import Position, UserPosition
from quberi_api.domain.spaces.models import Space
from quberi_api.services.errors import (
    AlreadyExistsError,
    InvalidInputError,
    NotFoundError,
)

# ---------------------------------------------------------------------------
# Result payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DirectManagerRef:
    user_id: uuid.UUID
    display_name: str


@dataclass(frozen=True)
class MemberView:
    """A user in an organisation, plus their org-level role code (if any)."""

    user: User
    role_code: str  # "" when no org-level RoleBinding is attached
    is_pending: bool
    primary_org_unit_id: Optional[uuid.UUID] = None
    primary_org_unit_name: Optional[str] = None
    primary_position_id: Optional[uuid.UUID] = None
    primary_position_name: Optional[str] = None
    direct_managers: tuple[DirectManagerRef, ...] = ()

    @classmethod
    def from_row(
        cls,
        user: User,
        role_code: Optional[str],
        primary_org_unit_id: Optional[uuid.UUID] = None,
        primary_org_unit_name: Optional[str] = None,
        primary_position_id: Optional[uuid.UUID] = None,
        primary_position_name: Optional[str] = None,
        direct_managers: tuple[DirectManagerRef, ...] = (),
    ) -> MemberView:
        return cls(
            user=user,
            role_code=role_code or "",
            is_pending=user.status == "invited",
            primary_org_unit_id=primary_org_unit_id,
            primary_org_unit_name=primary_org_unit_name,
            primary_position_id=primary_position_id,
            primary_position_name=primary_position_name,
            direct_managers=direct_managers,
        )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Only *organisation-level* assignable roles are accepted here.
#
# * ``owner`` is bootstrap-only — a new owner is minted via the admin API
#   when an organisation is created, never via invite.
# * ``editor`` / ``viewer`` describe *space-level* capabilities; granting them
#   on the organisation resource has no useful meaning, so we refuse them
#   here. They will be granted on individual spaces by a future
#   Space-members panel.
_INVITABLE_ROLE_CODES = frozenset({"admin", "member"})


def _validate_email(email: str) -> None:
    if not _EMAIL_RE.match(email):
        raise InvalidInputError(f"invalid email: {email!r}")


def _validate_role_code(code: str) -> None:
    if code not in SYSTEM_ROLE_CODES:
        raise InvalidInputError(f"unknown role: {code!r}")
    if code not in _INVITABLE_ROLE_CODES:
        raise InvalidInputError(
            f"role {code!r} is not assignable via invitation "
            f"(allowed: {sorted(_INVITABLE_ROLE_CODES)})"
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_members(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
) -> list[MemberView]:
    """Every user in the org, LEFT JOIN'd against their org-level role."""

    # Correlated LATERAL-ish subquery would be cleaner; a LEFT JOIN through
    # RoleBinding + Role for resource=organization is good enough for MVP
    # cardinality (small tenants). If a user somehow has multiple org-level
    # role bindings we pick the highest-priority one via the CASE below.
    role_priority = {
        "owner": 0,
        "admin": 1,
        "editor": 2,
        "viewer": 3,
    }

    # Two LEFT JOINs:
    #   * org-level RoleBinding + Role — to surface the caller's role code
    #   * primary UserOrganizationUnit + OrganizationUnit — to display the
    #     user's "所属部门" ("primary department") in list rendering
    stmt = (
        select(
            User,
            Role.code,
            UserOrganizationUnit.organization_unit_id,
            OrganizationUnit.name,
        )
        .join(
            RoleBinding,
            (RoleBinding.subject_type == "user")
            & (RoleBinding.subject_id == User.id)
            & (RoleBinding.resource_type == "organization")
            & (RoleBinding.resource_id == organization_id),
            isouter=True,
        )
        .join(Role, Role.id == RoleBinding.role_id, isouter=True)
        .join(
            UserOrganizationUnit,
            (UserOrganizationUnit.user_id == User.id)
            & (UserOrganizationUnit.is_primary.is_(True)),
            isouter=True,
        )
        .join(
            OrganizationUnit,
            OrganizationUnit.id == UserOrganizationUnit.organization_unit_id,
            isouter=True,
        )
        .where(User.organization_id == organization_id)
        .order_by(User.created_at.asc())
    )

    rows = list((await session.execute(stmt)).all())
    # Collapse duplicates from users with multiple org-role bindings by
    # keeping the highest-priority role we see. Primary-org-unit is unique
    # per user (partial unique index) so it never duplicates.
    best: dict[
        uuid.UUID,
        tuple[User, Optional[str], Optional[uuid.UUID], Optional[str]],
    ] = {}
    for user, role_code, primary_unit_id, primary_unit_name in rows:
        current = best.get(user.id)
        if current is None:
            best[user.id] = (user, role_code, primary_unit_id, primary_unit_name)
            continue
        _, existing_role, existing_unit_id, existing_unit_name = current
        existing_prio = role_priority.get(existing_role or "", 99)
        new_prio = role_priority.get(role_code or "", 99)
        if new_prio < existing_prio:
            best[user.id] = (
                user,
                role_code,
                primary_unit_id or existing_unit_id,
                primary_unit_name or existing_unit_name,
            )

    users = [(user, role, ou_id, ou_name) for user, role, ou_id, ou_name in
             sorted(best.values(), key=lambda t: t[0].created_at)]
    if not users:
        return []

    user_ids = [u[0].id for u in users]

    # Primary position per user (one row max thanks to the partial unique
    # index).
    primary_pos_stmt = (
        select(UserPosition.user_id, Position.id, Position.name)
        .join(Position, Position.id == UserPosition.position_id)
        .where(UserPosition.user_id.in_(user_ids))
        .where(UserPosition.organization_id == organization_id)
        .where(UserPosition.is_primary.is_(True))
        .where(UserPosition.end_date.is_(None))
    )
    primary_position: dict[uuid.UUID, tuple[uuid.UUID, str]] = {
        uid: (pid, pname)
        for uid, pid, pname in (await session.execute(primary_pos_stmt)).all()
    }

    # Direct managers: for each user, resolve their primary position's
    # ``reports_to_position_id`` → users currently occupying that manager
    # position (end_date IS NULL). Runs one query per user with a primary
    # position; for MVP cardinality that's acceptable.
    direct_managers_by_user: dict[uuid.UUID, list[DirectManagerRef]] = {}
    for uid, (pid, _) in primary_position.items():
        # Fetch the manager position id for this user's primary position.
        reports_to_stmt = select(Position.reports_to_position_id).where(
            Position.id == pid
        )
        manager_pos_id = (
            await session.execute(reports_to_stmt)
        ).scalar_one_or_none()
        if manager_pos_id is None:
            continue
        rows = (
            await session.execute(
                select(User.id, User.display_name)
                .join(UserPosition, UserPosition.user_id == User.id)
                .where(UserPosition.position_id == manager_pos_id)
                .where(UserPosition.end_date.is_(None))
                .where(UserPosition.organization_id == organization_id)
                .order_by(User.display_name.asc())
            )
        ).all()
        direct_managers_by_user[uid] = [
            DirectManagerRef(user_id=mid, display_name=mname) for mid, mname in rows
        ]

    return [
        MemberView.from_row(
            user,
            role_code,
            unit_id,
            unit_name,
            primary_position_id=primary_position.get(user.id, (None, None))[0]
            if user.id in primary_position
            else None,
            primary_position_name=primary_position.get(user.id, (None, None))[1]
            if user.id in primary_position
            else None,
            direct_managers=tuple(direct_managers_by_user.get(user.id, [])),
        )
        for user, role_code, unit_id, unit_name in users
    ]


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


async def invite_member(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    invited_by_user_id: uuid.UUID,
    email: str,
    display_name: str,
    role_code: str,
) -> MemberView:
    """Provision an invited user with a system role at the organisation level."""

    email = email.strip().lower()
    display_name = display_name.strip()
    role_code = role_code.strip().lower()

    _validate_email(email)
    _validate_role_code(role_code)
    if not display_name:
        raise InvalidInputError("display name is required")

    async with session.begin():
        role = await _load_system_role(session, role_code)
        identity = await _get_or_create_identity(session, email=email)

        # ``User`` unique constraint is (organization_id, identity_id) and a
        # separate (organization_id, email). Either fires as IntegrityError.
        existing_stmt = select(User).where(
            (User.organization_id == organization_id)
            & (
                (User.identity_id == identity.id)
                | (User.email == email)
            )
        )
        existing = (await session.execute(existing_stmt)).scalar_one_or_none()
        if existing is not None:
            raise AlreadyExistsError(
                f"user with email {email!r} is already a member of this organisation"
            )

        user = User(
            organization_id=organization_id,
            identity_id=identity.id,
            display_name=display_name,
            email=email,
            locale="en-US",
            timezone="UTC",
            status="invited",
            attributes={"invited_by": str(invited_by_user_id)},
        )
        session.add(user)
        try:
            await session.flush()
        except IntegrityError as exc:  # pragma: no cover - defensive
            raise AlreadyExistsError(
                f"user with email {email!r} is already a member of this organisation"
            ) from exc

        session.add(
            RoleBinding(
                organization_id=organization_id,
                role_id=role.id,
                subject_type="user",
                subject_id=user.id,
                resource_type="organization",
                resource_id=organization_id,
                conditions={},
                created_by_user_id=invited_by_user_id,
            )
        )

        # Auto-provision the invitee's personal space + Owner binding on
        # that space, so when they accept the invite the workspace already
        # has a place for their private drafts. Matches what
        # ``bootstrap_organization`` does for the first owner of a tenant.
        owner_role = await _load_system_role(session, "owner")
        personal_space = Space(
            organization_id=organization_id,
            name=f"{display_name}'s space",
            slug=f"personal-{user.id.hex[:12]}",
            description="Personal workspace for private drafts and experiments.",
            preset="personal",
            owner_type="user",
            owner_id=user.id,
            visibility="private",
            trust_level="trusted",
            inheritance_policy={},
            attributes={},
            status="active",
        )
        session.add(personal_space)
        await session.flush()

        session.add(
            RoleBinding(
                organization_id=organization_id,
                role_id=owner_role.id,
                subject_type="user",
                subject_id=user.id,
                resource_type="space",
                resource_id=personal_space.id,
                conditions={},
                created_by_user_id=invited_by_user_id,
            )
        )

        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=invited_by_user_id,
                action="member.invited",
                resource_type="user",
                resource_id=user.id,
                space_id=personal_space.id,
                payload={
                    "email": email,
                    "display_name": display_name,
                    "role": role_code,
                    "personal_space_id": str(personal_space.id),
                },
            )
        )
        await session.flush()
        # ``created_at`` / ``updated_at`` are server-defaulted — refresh so
        # response serialisers can read them outside the transaction.
        await session.refresh(user, attribute_names=["created_at", "updated_at"])

    return MemberView.from_row(user, role_code)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _load_system_role(session: AsyncSession, code: str) -> Role:
    # ``LIMIT 1`` + ``first()`` — robust against seed migrations that might
    # have accidentally been applied twice during downgrade/upgrade cycles.
    stmt = (
        select(Role)
        .where(
            Role.code == code,
            Role.is_system.is_(True),
            Role.organization_id.is_(None),
        )
        .order_by(Role.created_at.asc())
        .limit(1)
    )
    role = (await session.execute(stmt)).scalars().first()
    if role is None:
        raise NotFoundError(
            f"system role {code!r} not found — did migration 0002 run?"
        )
    return role


async def set_member_role(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    role_code: str,
) -> MemberView:
    """Change a member's organisation-level role.

    Only ``admin`` / ``member`` are settable here — ``owner`` is bootstrap-
    only (minted when the org is created) and can never be granted or
    revoked through this path, in either direction:

    * The target cannot already be ``owner`` (demoting the owner would
      leave the org without one — ownership transfer, if ever needed,
      is a deliberate separate operation, not a side effect of a role
      edit).
    * The caller cannot grant ``owner`` (not in ``_INVITABLE_ROLE_CODES``,
      so ``_validate_role_code`` already rejects it).
    """

    role_code = role_code.strip().lower()
    _validate_role_code(role_code)

    async with session.begin():
        user = (
            await session.execute(
                select(User).where(
                    User.id == target_user_id,
                    User.organization_id == organization_id,
                )
            )
        ).scalar_one_or_none()
        if user is None:
            raise NotFoundError("member not found in this organisation")

        existing_binding_stmt = (
            select(RoleBinding, Role.code)
            .join(Role, Role.id == RoleBinding.role_id)
            .where(
                RoleBinding.organization_id == organization_id,
                RoleBinding.resource_type == "organization",
                RoleBinding.resource_id == organization_id,
                RoleBinding.subject_type == "user",
                RoleBinding.subject_id == target_user_id,
            )
        )
        existing_rows = (await session.execute(existing_binding_stmt)).all()

        if any(code == "owner" for _, code in existing_rows):
            raise InvalidInputError(
                "the organisation owner's role cannot be changed here"
            )

        if target_user_id == actor_user_id:
            raise InvalidInputError("you cannot change your own role")

        # Replace every existing org-level binding this user holds with a
        # single binding for the new role — a user should have exactly one
        # org-level role at a time, so "set role" means "replace", not
        # "add on top of".
        for binding, _ in existing_rows:
            await session.delete(binding)
        await session.flush()

        role = await _load_system_role(session, role_code)
        session.add(
            RoleBinding(
                organization_id=organization_id,
                role_id=role.id,
                subject_type="user",
                subject_id=target_user_id,
                resource_type="organization",
                resource_id=organization_id,
                conditions={},
                created_by_user_id=actor_user_id,
            )
        )

        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="member.role_changed",
                resource_type="user",
                resource_id=target_user_id,
                payload={"new_role": role_code},
            )
        )

    return MemberView.from_row(user, role_code)


async def _get_or_create_identity(session: AsyncSession, *, email: str) -> Identity:
    stmt = select(Identity).where(Identity.primary_email == email)
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing

    identity = Identity(
        primary_email=email,
        primary_email_verified=False,
        status="active",
    )
    session.add(identity)
    await session.flush()
    return identity
