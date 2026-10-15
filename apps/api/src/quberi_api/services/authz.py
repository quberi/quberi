"""Authorization helpers used by services and API dependencies.

Public surface
--------------
* ``caller_has_org_role`` — quick "does the caller hold ``owner``/``admin`` on
  the organisation?" check. Only walks *direct* user bindings, which matches
  the semantics we want for admin gates: an admin capability should be
  granted intentionally to a specific user, not inherited by accident.
* ``resolve_effective_role_bindings`` — the full evaluator. Returns every
  ``RoleBinding`` that applies to ``(user, resource)`` after walking:
    - direct ``user`` bindings
    - bindings on positions the user currently occupies
    - bindings on org units the user is assigned to (and their ancestors)
    - bindings on groups the user belongs to via any literal path
      (user / position / org unit membership)
  Each returned binding carries the "hit path" it took so the UI can
  explain *why* a user has access.
* ``resolve_effective_role_codes`` — thin wrapper that flattens the above
  to a set of role code strings, for callers that only need "does the user
  have any role granting access?".
* ``list_accessible_space_ids`` — used by the spaces service to compute the
  set of spaces a user can see. One CTE-heavy query rather than iterating.

The evaluator is deliberately un-cached. Real deployments will pin a cache
layer in front once we have a stable enough hit-rate signal. Cardinality
today (< 10k users, < 100 groups, < 100 positions per org) makes an
uncached round-trip acceptable.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Select, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.authz.models import Role, RoleBinding
from quberi_api.domain.identity.models import Organization, User
from quberi_api.domain.org.models import (
    Group,
    GroupMember,
    OrganizationUnit,
    UserOrganizationUnit,
)
from quberi_api.domain.positions.models import Position, UserPosition
from quberi_api.domain.spaces.models import Space
from quberi_api.services.errors import NotFoundError

# ---------------------------------------------------------------------------
# Backwards-compatible helper
# ---------------------------------------------------------------------------


async def caller_has_org_role(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    allowed_codes: Iterable[str],
) -> bool:
    """``True`` iff the user holds any of ``allowed_codes`` **directly** on the org.

    Intentionally excludes group / position / org-unit inheritance — admin
    capabilities should be granted to a specific user with intent.
    """

    codes = tuple(allowed_codes)
    if not codes:
        return False

    stmt = (
        select(RoleBinding.id)
        .join(Role, Role.id == RoleBinding.role_id)
        .where(RoleBinding.organization_id == organization_id)
        .where(RoleBinding.resource_type == "organization")
        .where(RoleBinding.resource_id == organization_id)
        .where(RoleBinding.subject_type == "user")
        .where(RoleBinding.subject_id == user_id)
        .where(Role.code.in_(codes))
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


# Highest-priority-first; mirrors the ranking used elsewhere (members list,
# space access resolution) so "the user's role" always means the same thing.
_ORG_ROLE_PRIORITY: dict[str, int] = {"owner": 0, "admin": 1, "member": 2}


async def get_direct_org_role(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> str:
    """The user's direct organisation-level role code, or ``""`` if none.

    Same intentional restriction as :func:`caller_has_org_role` — only
    *direct* ``user`` bindings count, no group/position/org-unit
    inheritance. This is what the UI uses to decide whether to show the
    "admin console" entry point, so it must mean "granted to this person,
    on purpose" and nothing fuzzier. If a user somehow holds more than one
    org-level binding, the highest-priority role wins.
    """

    stmt = (
        select(Role.code)
        .join(RoleBinding, RoleBinding.role_id == Role.id)
        .where(RoleBinding.organization_id == organization_id)
        .where(RoleBinding.resource_type == "organization")
        .where(RoleBinding.resource_id == organization_id)
        .where(RoleBinding.subject_type == "user")
        .where(RoleBinding.subject_id == user_id)
    )
    codes = [row[0] for row in (await session.execute(stmt)).all()]
    if not codes:
        return ""
    return min(codes, key=lambda c: _ORG_ROLE_PRIORITY.get(c, 99))


# ---------------------------------------------------------------------------
# Effective-binding evaluator
# ---------------------------------------------------------------------------


HitPath = Literal[
    "direct",
    "position",
    "organization_unit",
    "organization_unit_ancestor",
    "group_via_user",
    "group_via_position",
    "group_via_organization_unit",
    "organization",
]


@dataclass(frozen=True)
class EffectiveBinding:
    """One resolved ``RoleBinding`` + the path that made it apply."""

    binding: RoleBinding
    role_code: str
    hit_path: HitPath


async def _user_position_ids(
    session: AsyncSession, *, user_id: uuid.UUID, organization_id: uuid.UUID
) -> list[uuid.UUID]:
    stmt = (
        select(UserPosition.position_id)
        .where(UserPosition.user_id == user_id)
        .where(UserPosition.organization_id == organization_id)
        .where(UserPosition.end_date.is_(None))
    )
    return list((await session.execute(stmt)).scalars().all())


async def _user_org_unit_ids(
    session: AsyncSession, *, user_id: uuid.UUID, organization_id: uuid.UUID
) -> list[uuid.UUID]:
    stmt = (
        select(UserOrganizationUnit.organization_unit_id)
        .where(UserOrganizationUnit.user_id == user_id)
        .where(UserOrganizationUnit.organization_id == organization_id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _org_unit_ancestor_ids(
    session: AsyncSession,
    *,
    seed_unit_ids: list[uuid.UUID],
    organization_id: uuid.UUID,
) -> set[uuid.UUID]:
    """Return the closure of ancestors of every seed (excluding seeds themselves).

    A recursive CTE walks ``parent_id`` up until we hit the roots.
    """

    if not seed_unit_ids:
        return set()

    seed_cte = (
        select(OrganizationUnit.parent_id.label("id"))
        .where(OrganizationUnit.id.in_(seed_unit_ids))
        .where(OrganizationUnit.parent_id.is_not(None))
    ).cte(recursive=True)

    parent_alias = seed_cte.alias("p")
    seed_cte = seed_cte.union_all(
        select(OrganizationUnit.parent_id.label("id"))
        .join(parent_alias, OrganizationUnit.id == parent_alias.c.id)
        .where(OrganizationUnit.parent_id.is_not(None))
        .where(OrganizationUnit.organization_id == organization_id)
    )
    rows = await session.execute(select(seed_cte.c.id))
    return {row for row in rows.scalars().all() if row is not None}


async def _group_ids_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    position_ids: list[uuid.UUID],
    org_unit_ids: list[uuid.UUID],
) -> dict[uuid.UUID, HitPath]:
    """Return every group id the user belongs to via any literal path.

    Dict value indicates the *strongest* hit path we care about for the
    UI ("direct" wins over "position" wins over "org unit"). Ancestor
    org-unit paths are not walked here — group membership is literal.
    """

    hits: dict[uuid.UUID, HitPath] = {}

    direct_rows = (
        await session.execute(
            select(GroupMember.group_id).where(
                GroupMember.member_type == "user",
                GroupMember.member_id == user_id,
            )
        )
    ).scalars().all()
    for gid in direct_rows:
        hits[gid] = "group_via_user"

    if position_ids:
        rows = (
            await session.execute(
                select(GroupMember.group_id).where(
                    GroupMember.member_type == "position",
                    GroupMember.member_id.in_(position_ids),
                )
            )
        ).scalars().all()
        for gid in rows:
            hits.setdefault(gid, "group_via_position")

    if org_unit_ids:
        rows = (
            await session.execute(
                select(GroupMember.group_id).where(
                    GroupMember.member_type == "organization_unit",
                    GroupMember.member_id.in_(org_unit_ids),
                )
            )
        ).scalars().all()
        for gid in rows:
            hits.setdefault(gid, "group_via_organization_unit")

    return hits


async def resolve_effective_role_bindings(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    resource_type: str,
    resource_id: uuid.UUID,
) -> list[EffectiveBinding]:
    """Return every ``RoleBinding`` that applies to ``(user, resource)``.

    Behaviour
    ---------
    * A binding may hit through multiple paths (e.g., user is directly bound
      *and* their group is bound). We dedupe by ``RoleBinding.id`` and keep
      the *strongest* path ("direct" > "position" > "org unit" > "group").
    * ``resource_type`` matches literally. ``resource_id`` is checked exactly;
      inherited access from a parent space or org is out of scope until we
      add ``inheritance_policy`` evaluation.
    """

    position_ids = await _user_position_ids(
        session, user_id=user_id, organization_id=organization_id
    )
    direct_ou_ids = await _user_org_unit_ids(
        session, user_id=user_id, organization_id=organization_id
    )
    ancestor_ou_ids = await _org_unit_ancestor_ids(
        session, seed_unit_ids=direct_ou_ids, organization_id=organization_id
    )
    all_ou_ids = set(direct_ou_ids) | ancestor_ou_ids
    group_hits = await _group_ids_for_user(
        session,
        user_id=user_id,
        organization_id=organization_id,
        position_ids=position_ids,
        org_unit_ids=direct_ou_ids,
    )

    # Load candidate bindings across all four subject types in one query.
    stmt: Select = (
        select(RoleBinding, Role.code)
        .join(Role, Role.id == RoleBinding.role_id)
        .where(RoleBinding.organization_id == organization_id)
        .where(RoleBinding.resource_type == resource_type)
        .where(RoleBinding.resource_id == resource_id)
    )
    rows = (await session.execute(stmt)).all()

    # Path precedence: lower rank = stronger, wins ties.
    rank: dict[HitPath, int] = {
        "direct": 0,
        "position": 1,
        "organization_unit": 2,
        "organization_unit_ancestor": 3,
        "group_via_user": 4,
        "group_via_position": 5,
        "group_via_organization_unit": 6,
        "organization": 7,
    }

    accepted: dict[uuid.UUID, EffectiveBinding] = {}

    def _record(binding: RoleBinding, code: str, path: HitPath) -> None:
        existing = accepted.get(binding.id)
        if existing is None or rank[path] < rank[existing.hit_path]:
            accepted[binding.id] = EffectiveBinding(
                binding=binding, role_code=code, hit_path=path
            )

    for binding, role_code in rows:
        if binding.subject_type == "user":
            if binding.subject_id == user_id:
                _record(binding, role_code, "direct")
            continue
        if binding.subject_type == "position":
            if binding.subject_id in position_ids:
                _record(binding, role_code, "position")
            continue
        if binding.subject_type == "organization_unit":
            if binding.subject_id in direct_ou_ids:
                _record(binding, role_code, "organization_unit")
            elif binding.subject_id in ancestor_ou_ids:
                _record(binding, role_code, "organization_unit_ancestor")
            continue
        if binding.subject_type == "group":
            path = group_hits.get(binding.subject_id)
            if path is not None:
                _record(binding, role_code, path)
            continue
        if binding.subject_type == "organization":
            # Everyone in the organisation. ``organization_id`` is the
            # caller's own org (every binding above is already filtered to
            # it), so matching the id is enough.
            if binding.subject_id == organization_id:
                _record(binding, role_code, "organization")
            continue
        # service_account and future subject types: skip — we resolve for a
        # human user, not a machine principal.

    # Also silence lint about ``all_ou_ids`` being unused if not referenced.
    _ = all_ou_ids
    return list(accepted.values())


async def resolve_effective_role_codes(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    resource_type: str,
    resource_id: uuid.UUID,
) -> set[str]:
    bindings = await resolve_effective_role_bindings(
        session,
        user_id=user_id,
        organization_id=organization_id,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    return {b.role_code for b in bindings}


# ---------------------------------------------------------------------------
# Space visibility helper (called from services.spaces)
# ---------------------------------------------------------------------------


async def list_accessible_space_ids(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> set[uuid.UUID]:
    """Return the set of space ids the user can see.

    A space is visible when the user either owns it directly (personal
    spaces) OR any ``RoleBinding`` with ``resource_type='space'`` matches
    them via the same four paths as the full evaluator.
    """

    position_ids = await _user_position_ids(
        session, user_id=user_id, organization_id=organization_id
    )
    direct_ou_ids = await _user_org_unit_ids(
        session, user_id=user_id, organization_id=organization_id
    )
    ancestor_ou_ids = await _org_unit_ancestor_ids(
        session, seed_unit_ids=direct_ou_ids, organization_id=organization_id
    )
    all_ou_ids = list(set(direct_ou_ids) | ancestor_ou_ids)
    group_hits = await _group_ids_for_user(
        session,
        user_id=user_id,
        organization_id=organization_id,
        position_ids=position_ids,
        org_unit_ids=direct_ou_ids,
    )
    group_ids = list(group_hits.keys())

    subject_predicates = [
        select(Space.id).where(
            Space.organization_id == organization_id,
            Space.owner_type == "user",
            Space.owner_id == user_id,
        ),
        select(RoleBinding.resource_id).where(
            RoleBinding.organization_id == organization_id,
            RoleBinding.resource_type == "space",
            RoleBinding.subject_type == "user",
            RoleBinding.subject_id == user_id,
        ),
    ]
    if position_ids:
        subject_predicates.append(
            select(RoleBinding.resource_id).where(
                RoleBinding.organization_id == organization_id,
                RoleBinding.resource_type == "space",
                RoleBinding.subject_type == "position",
                RoleBinding.subject_id.in_(position_ids),
            )
        )
    if all_ou_ids:
        subject_predicates.append(
            select(RoleBinding.resource_id).where(
                RoleBinding.organization_id == organization_id,
                RoleBinding.resource_type == "space",
                RoleBinding.subject_type == "organization_unit",
                RoleBinding.subject_id.in_(all_ou_ids),
            )
        )
    if group_ids:
        subject_predicates.append(
            select(RoleBinding.resource_id).where(
                RoleBinding.organization_id == organization_id,
                RoleBinding.resource_type == "space",
                RoleBinding.subject_type == "group",
                RoleBinding.subject_id.in_(group_ids),
            )
        )
    # Spaces shared with the whole organisation.
    subject_predicates.append(
        select(RoleBinding.resource_id).where(
            RoleBinding.organization_id == organization_id,
            RoleBinding.resource_type == "space",
            RoleBinding.subject_type == "organization",
            RoleBinding.subject_id == organization_id,
        )
    )

    unioned = union_all(*subject_predicates).subquery()
    rows = await session.execute(select(unioned.c[0]).distinct())
    return {row for row in rows.scalars().all() if row is not None}


# ---------------------------------------------------------------------------
# Reverse expansion: who has effective access to a space?
# ---------------------------------------------------------------------------


# The subject_type stored on ``RoleBinding.subject_type`` doesn't yet cover
# the "space itself is the source" case — that comes from ``Space.owner_id``
# handling elsewhere. In practice every space also gets an explicit
# ``RoleBinding(subject_type='user', role=owner)`` at creation time
# (see ``services.spaces.create_collaborative_space`` and
# ``services.members.invite_member``), so listing effective members via
# bindings alone is sufficient.


@dataclass(frozen=True)
class EffectiveHitPath:
    """One reason a user has access to a space.

    ``subject_type`` / ``subject_id`` / ``subject_display_name`` identify
    the *binding subject* that granted the access — the position the user
    holds, the group they belong to, the org unit they sit in, or the user
    themselves (direct binding).
    """

    hit_path: HitPath
    role_code: str
    subject_type: str
    subject_id: uuid.UUID
    subject_display_name: str


@dataclass(frozen=True)
class EffectiveMemberView:
    """A user's effective access to a specific space.

    ``effective_role`` is the highest-priority ``role_code`` seen across
    ``hit_paths``. ``hit_paths`` is preserved so the UI can render *why*.
    """

    user_id: uuid.UUID
    display_name: str
    email: str
    effective_role: str
    hit_paths: tuple[EffectiveHitPath, ...]


# Space-level role priority. Lower = stronger. ``member`` is included for
# completeness even though it's not assignable at space level (the
# validator in ``space_bindings`` rejects it) — a stray legacy binding
# should still resolve deterministically.
_ROLE_PRIORITY: dict[str, int] = {
    "owner": 0,
    "admin": 1,
    "editor": 2,
    "viewer": 3,
    "member": 4,
}


async def _descendant_units_by_seed(
    session: AsyncSession,
    *,
    seed_unit_ids: Iterable[uuid.UUID],
    organization_id: uuid.UUID,
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """Return ``{seed_id: {seed_id and every descendant id}}``.

    Uses one recursive CTE that carries the seed identity through each
    level, so we don't need N queries. Descendants are inclusive of the
    seed itself — a binding on unit X applies to users directly in X too.
    """

    seed_ids = [sid for sid in seed_unit_ids]
    if not seed_ids:
        return {}

    seed_cte = (
        select(
            OrganizationUnit.id.label("unit_id"),
            OrganizationUnit.id.label("seed_id"),
        )
        .where(OrganizationUnit.id.in_(seed_ids))
        .where(OrganizationUnit.organization_id == organization_id)
    ).cte(recursive=True, name="ou_desc")

    parent_alias = seed_cte.alias("p")
    seed_cte = seed_cte.union_all(
        select(
            OrganizationUnit.id.label("unit_id"),
            parent_alias.c.seed_id,
        )
        .join(parent_alias, OrganizationUnit.parent_id == parent_alias.c.unit_id)
        .where(OrganizationUnit.organization_id == organization_id)
    )

    rows = (
        await session.execute(select(seed_cte.c.seed_id, seed_cte.c.unit_id))
    ).all()

    out: dict[uuid.UUID, set[uuid.UUID]] = {}
    for seed_id, unit_id in rows:
        out.setdefault(seed_id, set()).add(unit_id)
    return out


async def list_effective_space_members(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
) -> list[EffectiveMemberView]:
    """Every user who has any effective access to a space, with hit paths.

    Design
    ------
    Forward walk from the space's bindings outward, expanding each subject
    to the users it grants access to:

      * ``subject_type='user'``  → the single user, hit_path ``direct``
      * ``subject_type='position'`` → users with an open ``UserPosition``
        row for that position, hit_path ``position``
      * ``subject_type='organization_unit'`` → users directly in the unit
        (hit_path ``organization_unit``) plus users in any descendant unit
        (hit_path ``organization_unit_ancestor``)
      * ``subject_type='group'`` → each ``GroupMember`` row is expanded
        literally:
          - ``member_type='user'``              → hit_path ``group_via_user``
          - ``member_type='position'``          → occupants of that position,
            hit_path ``group_via_position``
          - ``member_type='organization_unit'`` → direct members of that
            unit (no ancestor/descendant walk — group membership is
            literal, per Slice B design), hit_path
            ``group_via_organization_unit``
          - ``member_type='service_account'``   → skipped (not a human)

    All paths keep the RoleBinding's role code; the *effective* role per
    user is the highest priority among their paths.
    """

    # 1. Confirm the space exists (and belongs to this org) so callers see
    #    a clean 404 rather than an empty list for typo'd ids.
    space_stmt = select(Space).where(
        Space.id == space_id,
        Space.organization_id == organization_id,
    )
    space = (await session.execute(space_stmt)).scalar_one_or_none()
    if space is None:
        raise NotFoundError(f"space not found: {space_id}")

    # 2. Load every binding on this space, joined to its role code.
    bindings_stmt = (
        select(RoleBinding, Role.code)
        .join(Role, Role.id == RoleBinding.role_id)
        .where(RoleBinding.organization_id == organization_id)
        .where(RoleBinding.resource_type == "space")
        .where(RoleBinding.resource_id == space_id)
    )
    binding_rows = (await session.execute(bindings_stmt)).all()

    # Pre-bucket by subject type so we can batch expansions per type.
    user_subj: list[tuple[RoleBinding, str]] = []
    position_subj: list[tuple[RoleBinding, str]] = []
    unit_subj: list[tuple[RoleBinding, str]] = []
    group_subj: list[tuple[RoleBinding, str]] = []
    org_subj: list[tuple[RoleBinding, str]] = []
    for binding, role_code in binding_rows:
        if binding.subject_type == "user":
            user_subj.append((binding, role_code))
        elif binding.subject_type == "position":
            position_subj.append((binding, role_code))
        elif binding.subject_type == "organization_unit":
            unit_subj.append((binding, role_code))
        elif binding.subject_type == "group":
            group_subj.append((binding, role_code))
        elif (
            binding.subject_type == "organization"
            and binding.subject_id == organization_id
        ):
            org_subj.append((binding, role_code))
        # ``service_account`` bindings don't map to humans; ignore.

    # ---------- 3. Batch-load subject display names ----------
    position_names: dict[uuid.UUID, str] = {}
    if position_subj:
        pos_ids = [b.subject_id for b, _ in position_subj]
        rows = (
            await session.execute(
                select(Position.id, Position.name).where(Position.id.in_(pos_ids))
            )
        ).all()
        position_names = {pid: name for pid, name in rows}

    unit_names: dict[uuid.UUID, str] = {}
    if unit_subj:
        unit_ids = [b.subject_id for b, _ in unit_subj]
        rows = (
            await session.execute(
                select(OrganizationUnit.id, OrganizationUnit.name).where(
                    OrganizationUnit.id.in_(unit_ids)
                )
            )
        ).all()
        unit_names = {uid: name for uid, name in rows}

    group_names: dict[uuid.UUID, str] = {}
    if group_subj:
        grp_ids = [b.subject_id for b, _ in group_subj]
        rows = (
            await session.execute(
                select(Group.id, Group.name).where(Group.id.in_(grp_ids))
            )
        ).all()
        group_names = {gid: name for gid, name in rows}

    # ---------- 4. Expand subjects → users, accumulating hit paths ----------
    user_paths: dict[uuid.UUID, list[EffectiveHitPath]] = {}

    def _record(
        user_id: uuid.UUID,
        *,
        hit_path: HitPath,
        role_code: str,
        subject_type: str,
        subject_id: uuid.UUID,
        subject_display_name: str,
    ) -> None:
        user_paths.setdefault(user_id, []).append(
            EffectiveHitPath(
                hit_path=hit_path,
                role_code=role_code,
                subject_type=subject_type,
                subject_id=subject_id,
                subject_display_name=subject_display_name,
            )
        )

    # 4a. Direct user bindings.
    for binding, role_code in user_subj:
        _record(
            binding.subject_id,
            hit_path="direct",
            role_code=role_code,
            subject_type="user",
            subject_id=binding.subject_id,
            subject_display_name="",  # subject IS the user; UI can omit
        )

    # 4b. Position bindings → current occupants.
    if position_subj:
        pos_ids = [b.subject_id for b, _ in position_subj]
        rows = (
            await session.execute(
                select(UserPosition.position_id, UserPosition.user_id)
                .where(UserPosition.position_id.in_(pos_ids))
                .where(UserPosition.organization_id == organization_id)
                .where(UserPosition.end_date.is_(None))
            )
        ).all()
        occupants_by_pos: dict[uuid.UUID, list[uuid.UUID]] = {}
        for pid, uid in rows:
            occupants_by_pos.setdefault(pid, []).append(uid)
        for binding, role_code in position_subj:
            name = position_names.get(binding.subject_id, "")
            for uid in occupants_by_pos.get(binding.subject_id, ()):
                _record(
                    uid,
                    hit_path="position",
                    role_code=role_code,
                    subject_type="position",
                    subject_id=binding.subject_id,
                    subject_display_name=name,
                )

    # 4c. Organization-unit bindings → users in the unit *and* its
    #     descendants. A user directly in the bound unit hits with
    #     ``organization_unit``; a user in a descendant hits with
    #     ``organization_unit_ancestor`` (mirroring the forward evaluator).
    if unit_subj:
        seed_ids = [b.subject_id for b, _ in unit_subj]
        desc_map = await _descendant_units_by_seed(
            session,
            seed_unit_ids=seed_ids,
            organization_id=organization_id,
        )
        # Gather every unit_id we need to lookup users in.
        all_expanded_units: set[uuid.UUID] = set()
        for desc_set in desc_map.values():
            all_expanded_units.update(desc_set)
        # user_id -> set of unit_ids the user is directly in.
        user_units_map: dict[uuid.UUID, set[uuid.UUID]] = {}
        if all_expanded_units:
            urows = (
                await session.execute(
                    select(
                        UserOrganizationUnit.organization_unit_id,
                        UserOrganizationUnit.user_id,
                    ).where(
                        UserOrganizationUnit.organization_unit_id.in_(
                            all_expanded_units
                        )
                    )
                )
            ).all()
            unit_direct_members: dict[uuid.UUID, list[uuid.UUID]] = {}
            for oid, uid in urows:
                unit_direct_members.setdefault(oid, []).append(uid)
                user_units_map.setdefault(uid, set()).add(oid)
        else:
            unit_direct_members = {}

        for binding, role_code in unit_subj:
            seed_id = binding.subject_id
            name = unit_names.get(seed_id, "")
            desc_ids = desc_map.get(seed_id, {seed_id})
            for unit_id in desc_ids:
                for uid in unit_direct_members.get(unit_id, ()):
                    if unit_id == seed_id:
                        path: HitPath = "organization_unit"
                    else:
                        path = "organization_unit_ancestor"
                    _record(
                        uid,
                        hit_path=path,
                        role_code=role_code,
                        subject_type="organization_unit",
                        subject_id=seed_id,
                        subject_display_name=name,
                    )

    # 4d. Group bindings → literal members expanded.
    if group_subj:
        grp_ids = [b.subject_id for b, _ in group_subj]
        m_rows = (
            await session.execute(
                select(
                    GroupMember.group_id,
                    GroupMember.member_type,
                    GroupMember.member_id,
                ).where(GroupMember.group_id.in_(grp_ids))
            )
        ).all()
        members_by_group: dict[uuid.UUID, list[tuple[str, uuid.UUID]]] = {}
        pos_via_group: set[uuid.UUID] = set()
        unit_via_group: set[uuid.UUID] = set()
        for gid, mtype, mid in m_rows:
            members_by_group.setdefault(gid, []).append((mtype, mid))
            if mtype == "position":
                pos_via_group.add(mid)
            elif mtype == "organization_unit":
                unit_via_group.add(mid)

        occupants_via_group: dict[uuid.UUID, list[uuid.UUID]] = {}
        if pos_via_group:
            rows = (
                await session.execute(
                    select(UserPosition.position_id, UserPosition.user_id)
                    .where(UserPosition.position_id.in_(pos_via_group))
                    .where(UserPosition.organization_id == organization_id)
                    .where(UserPosition.end_date.is_(None))
                )
            ).all()
            for pid, uid in rows:
                occupants_via_group.setdefault(pid, []).append(uid)

        unit_members_via_group: dict[uuid.UUID, list[uuid.UUID]] = {}
        if unit_via_group:
            rows = (
                await session.execute(
                    select(
                        UserOrganizationUnit.organization_unit_id,
                        UserOrganizationUnit.user_id,
                    ).where(
                        UserOrganizationUnit.organization_unit_id.in_(unit_via_group)
                    )
                )
            ).all()
            for oid, uid in rows:
                unit_members_via_group.setdefault(oid, []).append(uid)

        for binding, role_code in group_subj:
            gid = binding.subject_id
            gname = group_names.get(gid, "")
            for mtype, mid in members_by_group.get(gid, ()):
                if mtype == "user":
                    _record(
                        mid,
                        hit_path="group_via_user",
                        role_code=role_code,
                        subject_type="group",
                        subject_id=gid,
                        subject_display_name=gname,
                    )
                elif mtype == "position":
                    for uid in occupants_via_group.get(mid, ()):
                        _record(
                            uid,
                            hit_path="group_via_position",
                            role_code=role_code,
                            subject_type="group",
                            subject_id=gid,
                            subject_display_name=gname,
                        )
                elif mtype == "organization_unit":
                    for uid in unit_members_via_group.get(mid, ()):
                        _record(
                            uid,
                            hit_path="group_via_organization_unit",
                            role_code=role_code,
                            subject_type="group",
                            subject_id=gid,
                            subject_display_name=gname,
                        )
                # ``service_account`` group members don't produce human hits.

    # 4e. Organisation-wide bindings → every active member of the org.
    if org_subj:
        org_name = (
            await session.execute(
                select(Organization.name).where(Organization.id == organization_id)
            )
        ).scalar_one_or_none() or ""
        active_user_ids = list(
            (
                await session.execute(
                    select(User.id).where(
                        User.organization_id == organization_id,
                        User.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
        for binding, role_code in org_subj:
            for uid in active_user_ids:
                _record(
                    uid,
                    hit_path="organization",
                    role_code=role_code,
                    subject_type="organization",
                    subject_id=organization_id,
                    subject_display_name=org_name,
                )

    if not user_paths:
        return []

    # ---------- 5. Materialise display names + effective roles ----------
    all_uids = list(user_paths.keys())
    user_rows = (
        await session.execute(
            select(User.id, User.display_name, User.email).where(
                User.id.in_(all_uids),
                User.organization_id == organization_id,
            )
        )
    ).all()
    users_by_id: dict[uuid.UUID, tuple[str, str]] = {
        uid: (name, email) for uid, name, email in user_rows
    }

    result: list[EffectiveMemberView] = []
    for uid, paths in user_paths.items():
        meta = users_by_id.get(uid)
        if meta is None:
            # User was deleted, migrated, or lives in a different org — skip.
            continue
        display_name, email = meta
        # Highest-priority role_code across all this user's hit paths.
        best_role = min(
            paths, key=lambda p: _ROLE_PRIORITY.get(p.role_code, 99)
        ).role_code
        result.append(
            EffectiveMemberView(
                user_id=uid,
                display_name=display_name,
                email=email,
                effective_role=best_role,
                hit_paths=tuple(paths),
            )
        )

    # Stable ordering: by effective role priority, then display name.
    result.sort(
        key=lambda v: (
            _ROLE_PRIORITY.get(v.effective_role, 99),
            v.display_name.lower(),
        )
    )
    return result
