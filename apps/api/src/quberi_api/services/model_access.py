"""Enterprise model-access governance.

Which principals (user / group / organization_unit / position) may — or
may not — use a specific ``(provider_binding, model_id)`` chat model.

Rule
----
* A model with **no** policy rows is open to the whole org.
* Any ``allow`` row makes it **restricted** to the listed principals.
* An ``deny`` row **excludes** that principal even if they are also
  covered by an ``allow`` grant through a different principal (e.g. a
  user who is personally denied but whose group is allowed). Deny always
  wins — evaluated as two passes: check deny first, short-circuit to
  "not allowed" on any match, only then check allow.
* Org owners/admins always bypass both passes (so an admin can't lock
  themselves out and can always administer).

Personal-tier orgs never create policies and their single user is the
org owner, so everything is naturally open — no tier special-casing
needed here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Iterable, Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.ai.models import ModelAccessPolicy
from quberi_api.services import authz

_ADMIN_CODES = ("owner", "admin")
_PRINCIPAL_TYPES = ("user", "group", "organization_unit", "position")


_EFFECTS = ("allow", "deny")


@dataclass(frozen=True)
class PolicyPrincipal:
    principal_type: str
    principal_id: uuid.UUID
    # "allow" (default) or "deny". Kept as a plain field rather than a
    # separate dataclass so existing call sites that only ever meant
    # "allow" don't need updating beyond adding the keyword where it
    # matters.
    effect: str = "allow"


# ---------------------------------------------------------------------------
# Membership resolution
# ---------------------------------------------------------------------------


async def _principals_for_user(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID
) -> dict[str, set[uuid.UUID]]:
    """Every principal id the user *is*, keyed by principal_type."""

    positions = set(
        await authz._user_position_ids(
            session, user_id=user_id, organization_id=organization_id
        )
    )
    org_units = set(
        await authz._user_org_unit_ids(
            session, user_id=user_id, organization_id=organization_id
        )
    )
    group_hits = await authz._group_ids_for_user(
        session,
        user_id=user_id,
        organization_id=organization_id,
        position_ids=list(positions),
        org_unit_ids=list(org_units),
    )
    return {
        "user": {user_id},
        "position": positions,
        "organization_unit": org_units,
        "group": set(group_hits.keys()),
    }


async def _is_org_admin(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    return await authz.caller_has_org_role(
        session,
        organization_id=organization_id,
        user_id=user_id,
        allowed_codes=_ADMIN_CODES,
    )


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------


async def is_user_allowed(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    binding_id: uuid.UUID,
    model_id: str,
) -> bool:
    rows = (
        await session.execute(
            select(ModelAccessPolicy).where(
                ModelAccessPolicy.organization_id == organization_id,
                ModelAccessPolicy.provider_binding_id == binding_id,
                ModelAccessPolicy.model_id == model_id,
            )
        )
    ).scalars().all()
    if not rows:
        return True
    if await _is_org_admin(session, organization_id=organization_id, user_id=user_id):
        return True
    princ = await _principals_for_user(
        session, organization_id=organization_id, user_id=user_id
    )

    def _hits(r: ModelAccessPolicy) -> bool:
        return r.principal_id in princ.get(r.principal_type, set())

    # Pass 1: any deny match short-circuits to "not allowed", regardless
    # of how many allow rows also match.
    if any(_hits(r) for r in rows if r.effect == "deny"):
        return False

    allow_rows = [r for r in rows if r.effect == "allow"]
    if not allow_rows:
        # Only deny rows exist (none matched this user) and no allow rows
        # at all → open to everyone, same as "no policy rows".
        return True

    # Pass 2: ordinary allow-list check.
    return any(_hits(r) for r in allow_rows)


async def filter_models_for_user(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    models: Sequence,
) -> list:
    """Filter a list of UsableChatModel by the user's access.

    ``models`` items need ``.binding_id`` and ``.model_id`` attributes.
    """

    if not models:
        return []
    if await _is_org_admin(session, organization_id=organization_id, user_id=user_id):
        return list(models)

    rows = (
        await session.execute(
            select(ModelAccessPolicy).where(
                ModelAccessPolicy.organization_id == organization_id
            )
        )
    ).scalars().all()
    by_model: dict[tuple[str, str], list[ModelAccessPolicy]] = {}
    for r in rows:
        by_model.setdefault((str(r.provider_binding_id), r.model_id), []).append(r)

    if not by_model:
        return list(models)  # nothing restricted anywhere

    princ = await _principals_for_user(
        session, organization_id=organization_id, user_id=user_id
    )

    def _hits(p: ModelAccessPolicy) -> bool:
        return p.principal_id in princ.get(p.principal_type, set())

    out = []
    for m in models:
        pols = by_model.get((str(m.binding_id), m.model_id))
        if not pols:
            out.append(m)  # open — no policy rows for this model at all
            continue
        if any(_hits(p) for p in pols if p.effect == "deny"):
            continue  # explicitly denied — wins over any allow match
        allow_pols = [p for p in pols if p.effect == "allow"]
        if not allow_pols or any(_hits(p) for p in allow_pols):
            out.append(m)
    return out


# ---------------------------------------------------------------------------
# Admin CRUD (replace-set semantics)
# ---------------------------------------------------------------------------


async def list_policies(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    binding_id: uuid.UUID,
    model_id: str,
) -> list[PolicyPrincipal]:
    rows = (
        await session.execute(
            select(ModelAccessPolicy).where(
                ModelAccessPolicy.organization_id == organization_id,
                ModelAccessPolicy.provider_binding_id == binding_id,
                ModelAccessPolicy.model_id == model_id,
            )
        )
    ).scalars().all()
    return [
        PolicyPrincipal(
            principal_type=r.principal_type,
            principal_id=r.principal_id,
            effect=r.effect,
        )
        for r in rows
    ]


async def set_policies(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    binding_id: uuid.UUID,
    model_id: str,
    principals: Iterable[PolicyPrincipal],
) -> None:
    """Replace the whole access set for a model. Empty = open to all.

    A given ``(principal_type, principal_id)`` may appear only once —
    allow and deny are mutually exclusive for the same subject (if the
    caller wants to flip someone from allowed to denied, they drop the
    allow row and add a deny row, not both at once; the UI enforces this
    by construction, but we validate here too in case of a stale/manual
    payload).
    """

    clean: list[PolicyPrincipal] = []
    seen: set[tuple[str, uuid.UUID]] = set()
    for p in principals:
        if p.principal_type not in _PRINCIPAL_TYPES:
            raise ValueError(f"invalid principal_type {p.principal_type!r}")
        if p.effect not in _EFFECTS:
            raise ValueError(f"invalid effect {p.effect!r} (allowed: {_EFFECTS})")
        key = (p.principal_type, p.principal_id)
        if key in seen:
            raise ValueError(
                f"principal {p.principal_type}:{p.principal_id} cannot be both "
                "allowed and denied — pick one"
            )
        seen.add(key)
        clean.append(p)

    await session.commit()  # release any autobegun read txn
    async with session.begin():
        await session.execute(
            delete(ModelAccessPolicy).where(
                ModelAccessPolicy.organization_id == organization_id,
                ModelAccessPolicy.provider_binding_id == binding_id,
                ModelAccessPolicy.model_id == model_id,
            )
        )
        for p in clean:
            session.add(
                ModelAccessPolicy(
                    organization_id=organization_id,
                    provider_binding_id=binding_id,
                    model_id=model_id,
                    principal_type=p.principal_type,
                    principal_id=p.principal_id,
                    effect=p.effect,
                )
            )


__all__ = [
    "PolicyPrincipal",
    "is_user_allowed",
    "filter_models_for_user",
    "list_policies",
    "set_policies",
]
