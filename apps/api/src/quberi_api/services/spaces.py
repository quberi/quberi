"""Space lifecycle service.

Scope for this slice
--------------------
* ``list_spaces_for_user`` — every space the caller can reach today. The
  visibility rule is intentionally simple:
      1. Spaces owned by the user directly (``owner_type='user'`` +
         ``owner_id=user_id``) — this is how personal spaces appear.
      2. Spaces where the user holds any direct ``RoleBinding``.
  Group / org-unit chains and ABAC evaluation are out of scope until a later
  slice.
* ``create_collaborative_space`` — creates a preset-``collaborative`` space,
  grants the creator the seeded ``owner`` role scoped to that space, writes
  an audit event. All in one transaction.
* ``get_space_for_user`` — same visibility rule as the list; raises
  ``NotFoundError`` if the caller cannot see it (deliberately not 403 —
  existence itself is scoped information).
* ``archive_space`` — sets ``status='archived'`` and writes an audit event.
  Only the space owner or someone with the ``owner`` role on the space can
  archive; the check is left to the endpoint for now via the ownership
  boolean returned from ``get_space_for_user``.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.authz.models import Role, RoleBinding
from quberi_api.domain.spaces.models import Space
from quberi_api.services import space_bindings
from quberi_api.services.authz import caller_has_org_role, list_accessible_space_ids
from quberi_api.services.errors import (
    AlreadyExistsError,
    InvalidInputError,
    NotFoundError,
    QuotaExceededError,
)

# Personal-tier orgs may hold at most this many active spaces (incl. the
# auto-provisioned personal space). Team/enterprise orgs are unlimited.
PERSONAL_MAX_SPACES = 20

# ---------------------------------------------------------------------------
# Result payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpaceView:
    """Denormalised space + caller-context flags."""

    space: Space
    is_owner: bool


@dataclass(frozen=True)
class SpaceGrant:
    """One initial access grant requested at space-creation time."""

    subject_type: str
    subject_id: uuid.UUID
    role: str


_OWNER_TYPES_CREATABLE = ("user", "organization")

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")


def _validate_slug(slug: str) -> None:
    if not _SLUG_RE.match(slug):
        raise InvalidInputError(
            "slug must be 1-80 chars, lowercase alphanumerics with internal hyphens"
        )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


async def list_spaces_for_user(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> list[SpaceView]:
    """Every space the user can see through any effective role binding.

    Delegates the "which space ids are reachable" question to the permission
    evaluator so all four paths (user / position / org unit + ancestors /
    group) are honoured. Personal-space ownership shows up as a direct hit
    on ``Space.owner_id`` inside ``list_accessible_space_ids``.
    """

    accessible = await list_accessible_space_ids(
        session, user_id=user_id, organization_id=organization_id
    )
    if not accessible:
        return []
    stmt = (
        select(Space)
        .where(Space.organization_id == organization_id)
        .where(Space.id.in_(accessible))
        .order_by(Space.preset.desc(), Space.created_at.asc())
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        SpaceView(space=row, is_owner=_is_owner(row, user_id=user_id))
        for row in rows
    ]


async def get_space_for_user(
    session: AsyncSession,
    *,
    space_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> SpaceView:
    accessible = await list_accessible_space_ids(
        session, user_id=user_id, organization_id=organization_id
    )
    if space_id not in accessible:
        raise NotFoundError(f"space not found: {space_id}")
    stmt = select(Space).where(
        Space.id == space_id, Space.organization_id == organization_id
    )
    space = (await session.execute(stmt)).scalar_one_or_none()
    if space is None:
        # Belt-and-braces: shouldn't happen given the accessible check above.
        raise NotFoundError(f"space not found: {space_id}")
    return SpaceView(space=space, is_owner=_is_owner(space, user_id=user_id))


def _is_owner(space: Space, *, user_id: uuid.UUID) -> bool:
    return space.owner_type == "user" and space.owner_id == user_id


async def resolve_target_space(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str | None = None,
) -> SpaceView:
    """Resolve a space to save into from a human-friendly reference.

    Used by the "save a note" flow where the caller (a person, or a model
    acting for them in an external agent) names a space in words rather than
    by id — or names nothing at all.

    * ``name is None``/blank → the caller's **personal space** (every user
      gets one at onboarding: ``preset='personal'``, owned by the user). If
      somehow absent, fall back to the first space they can reach.
    * ``name`` given → a case-insensitive, whitespace-trimmed match against
      the names of spaces the caller can reach. An exact (case-folded) match
      wins; otherwise raise ``NotFoundError`` listing nothing (existence is
      scoped information). Ambiguous duplicate names resolve to the
      earliest-created match, matching ``list_spaces_for_user`` ordering.

    Returns a :class:`SpaceView` so the caller can read both the id and the
    ownership flag. It does **not** check write permission — that stays with
    the write path (``documents.save_note`` → ``upload_local``'s editor+
    gate), so this resolver never leaks "you could write here" information.
    """

    spaces = await list_spaces_for_user(
        session, organization_id=organization_id, user_id=user_id
    )
    if not spaces:
        raise NotFoundError("no accessible space to save into")

    wanted = (name or "").strip()
    if not wanted:
        personal = next(
            (s for s in spaces if s.space.preset == "personal" and s.is_owner),
            None,
        )
        return personal or spaces[0]

    folded = wanted.casefold()
    match = next(
        (s for s in spaces if (s.space.name or "").strip().casefold() == folded),
        None,
    )
    if match is None:
        raise NotFoundError(f"no accessible space named {wanted!r}")
    return match


async def get_space_for_manager(
    session: AsyncSession,
    *,
    space_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> SpaceView:
    """Like :func:`get_space_for_user`, but for *managing* a space.

    An organisation owner/admin may manage an **organisation-owned** space
    even when no binding grants them access to it. This is deliberately
    only about administration (access bindings, archive); it does not make
    the space's documents or conversations readable — those still follow
    the normal access rules. Every other space keeps the strict
    "you must be able to see it" behaviour.
    """

    space = (
        await session.execute(
            select(Space).where(
                Space.id == space_id, Space.organization_id == organization_id
            )
        )
    ).scalar_one_or_none()
    if space is not None and space.owner_type == "organization":
        is_admin = await caller_has_org_role(
            session,
            organization_id=organization_id,
            user_id=user_id,
            allowed_codes=("owner", "admin"),
        )
        if is_admin:
            return SpaceView(space=space, is_owner=True)
    return await get_space_for_user(
        session,
        space_id=space_id,
        organization_id=organization_id,
        user_id=user_id,
    )


async def list_organization_spaces(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
) -> list[SpaceView]:
    """Every organisation-owned space, regardless of who can open it.

    Backs the admin console's Spaces page. The router restricts it to
    organisation owners/admins.
    """

    rows = list(
        (
            await session.execute(
                select(Space)
                .where(
                    Space.organization_id == organization_id,
                    Space.owner_type == "organization",
                )
                .order_by(Space.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return [SpaceView(space=row, is_owner=True) for row in rows]


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


async def create_collaborative_space(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    creator_user_id: uuid.UUID,
    name: str,
    slug: str,
    description: str | None = None,
    visibility: str = "private",
    trust_level: str = "guided",
    tier: str = "team",
    owner: str = "user",
    grants: Sequence[SpaceGrant] = (),
) -> SpaceView:
    """Create a collaborative space and make the creator its owner.

    ``tier`` is the org's product tier. Personal-tier orgs are capped at
    :data:`PERSONAL_MAX_SPACES` active spaces; team/enterprise are unlimited.

    ``owner`` decides who *holds* the space:

    * ``"user"`` (default) — the creator personally.
    * ``"organization"`` — the enterprise. The space outlives whoever
      created it and is managed by the organisation's owners/admins. The
      caller (router) is responsible for checking the creator is an org
      owner/admin; this function only enforces the tier.

    ``grants`` are initial access bindings (user / group / org unit /
    position / whole organisation, each with a role). They are inserted in
    the same transaction as the space, so either everything is created or
    nothing is — a bad subject id never leaves a half-shared space behind.
    """

    name = name.strip()
    slug = slug.strip().lower()
    if not name:
        raise InvalidInputError("space name is required")
    _validate_slug(slug)
    if visibility not in ("private", "organization", "public_link"):
        raise InvalidInputError(f"invalid visibility: {visibility!r}")
    if trust_level not in ("trusted", "guided", "exploratory"):
        raise InvalidInputError(f"invalid trust_level: {trust_level!r}")
    if owner not in _OWNER_TYPES_CREATABLE:
        raise InvalidInputError(f"invalid owner: {owner!r}")
    if owner == "organization" and tier == "personal":
        raise InvalidInputError(
            "personal accounts cannot create organisation-owned spaces"
        )

    async with session.begin():
        # Personal-tier space cap. Counts every active space in the org
        # (including the auto-provisioned personal one) against the limit.
        if tier == "personal":
            active_count = (
                await session.execute(
                    select(func.count())
                    .select_from(Space)
                    .where(
                        Space.organization_id == organization_id,
                        Space.status == "active",
                    )
                )
            ).scalar_one()
            if active_count >= PERSONAL_MAX_SPACES:
                raise QuotaExceededError(
                    f"personal accounts are limited to {PERSONAL_MAX_SPACES} spaces"
                )

        owner_role = await _load_system_role(session, "owner")

        space = Space(
            organization_id=organization_id,
            name=name,
            slug=slug,
            description=description,
            preset="collaborative",
            owner_type=owner,
            owner_id=creator_user_id if owner == "user" else organization_id,
            visibility=visibility,
            trust_level=trust_level,
            inheritance_policy={},
            attributes={},
            status="active",
        )
        session.add(space)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise AlreadyExistsError(
                f"space slug already exists in this organisation: {slug!r}"
            ) from exc

        binding = RoleBinding(
            organization_id=organization_id,
            role_id=owner_role.id,
            subject_type="user",
            subject_id=creator_user_id,
            resource_type="space",
            resource_id=space.id,
            conditions={},
        )
        session.add(binding)

        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=creator_user_id,
                action="space.created",
                resource_type="space",
                resource_id=space.id,
                space_id=space.id,
                payload={
                    "preset": "collaborative",
                    "visibility": visibility,
                    "trust_level": trust_level,
                    "slug": slug,
                    "owner_type": owner,
                },
            )
        )
        await session.flush()

        # Initial grants. The creator already holds ``owner`` above, so a
        # repeat of that exact grant is skipped rather than rejected; any
        # other duplicate in the request is collapsed.
        seen: set[tuple[str, uuid.UUID, str]] = {
            ("user", creator_user_id, "owner")
        }
        for grant in grants:
            key = (grant.subject_type, grant.subject_id, grant.role)
            if key in seen:
                continue
            seen.add(key)
            await space_bindings.grant_in_transaction(
                session,
                space=space,
                organization_id=organization_id,
                actor_user_id=creator_user_id,
                subject_type=grant.subject_type,
                subject_id=grant.subject_id,
                role_code=grant.role,
            )

        # Pull server-side timestamps (``created_at`` / ``updated_at``) so the
        # instance is fully populated before the transaction closes.
        await session.refresh(space, attribute_names=["created_at", "updated_at"])

    return SpaceView(space=space, is_owner=True)


async def archive_space(
    session: AsyncSession,
    *,
    space_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> SpaceView:
    """Set a space to ``status='archived'`` and record an audit event."""

    async with session.begin():
        view = await get_space_for_manager(
            session,
            space_id=space_id,
            organization_id=organization_id,
            user_id=actor_user_id,
        )
        if not view.is_owner:
            # Non-owners cannot archive today. Once we have a role/permission
            # evaluator this becomes a proper ``space:manage`` check.
            raise NotFoundError(f"space not found: {space_id}")

        space = view.space
        if space.status == "archived":
            # Idempotent: the row is already archived, don't add another audit
            # event.
            return view

        space.status = "archived"

        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=actor_user_id,
                action="space.archived",
                resource_type="space",
                resource_id=space.id,
                space_id=space.id,
                payload={"slug": space.slug, "preset": space.preset},
            )
        )
        await session.flush()
        # ``updated_at`` has an ``onupdate=func.now()`` server-side default;
        # the ORM marks it as stale after the UPDATE. Refresh inside the
        # async context so serialisers can read it after ``session.begin()``
        # closes.
        await session.refresh(space, attribute_names=["updated_at"])

    return SpaceView(space=space, is_owner=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_system_role(session: AsyncSession, code: str) -> Role:
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
