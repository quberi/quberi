"""Built-in read tools over knowledge + structured org data.

Every tool here is a thin, permission-preserving adapter over an existing
service. None of them writes. The point of the first agent slice is
*cross-source reading*: chaining a semantic knowledge search with lookups
against the org chart, positions, spaces, and access lists to answer
questions a single RAG pass cannot.

Serialisation contract
-----------------------
Each ``run`` returns plain JSON-serialisable Python (dicts / lists /
scalars). The runner json-dumps the result straight into the tool
observation the model reads, so we return **ids + human labels**, never
ORM rows, and we keep payloads compact (the model pays per token for
every observation).
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from quberi_api.services import (
    members as members_service,
    org_units as org_units_service,
    positions as positions_service,
    spaces as spaces_service,
)
from quberi_api.services.agents.base import Tool, ToolContext, ToolError
from quberi_api.services.authz import list_effective_space_members
from quberi_api.services.errors import NotFoundError
from quberi_api.services.rag.retrieve import retrieve_with_scope


# ---------------------------------------------------------------------------
# Shared resolution helpers
# ---------------------------------------------------------------------------


def _as_uuid(value: str) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


async def _resolve_accessible_space(
    ctx: ToolContext, identifier: str
) -> spaces_service.SpaceView:
    """Resolve a space by id, slug, or (case-insensitive) name.

    Only searches spaces the caller can see, so this doubles as the
    access gate — an unknown or inaccessible space raises ``ToolError``
    with the same wording, never leaking which case it was.
    """

    identifier = (identifier or "").strip()
    if not identifier:
        raise ToolError("space identifier is required")

    views = await spaces_service.list_spaces_for_user(
        ctx.session,
        organization_id=ctx.organization_id,
        user_id=ctx.user_id,
    )
    wanted_id = _as_uuid(identifier)
    lowered = identifier.lower()
    for v in views:
        if wanted_id is not None and v.space.id == wanted_id:
            return v
        if v.space.slug.lower() == lowered or v.space.name.lower() == lowered:
            return v
    # Fall back to a substring match on name so "engineering" finds
    # "Engineering Wiki" — but only if it's unambiguous.
    partial = [v for v in views if lowered in v.space.name.lower()]
    if len(partial) == 1:
        return partial[0]
    raise ToolError(
        f"no accessible space matches {identifier!r} "
        f"(use list_spaces to see available spaces)"
    )


async def _resolve_org_unit(
    ctx: ToolContext, identifier: str
) -> org_units_service.OrgUnitView:
    identifier = (identifier or "").strip()
    if not identifier:
        raise ToolError("org unit identifier is required")
    units = await org_units_service.list_tree(
        ctx.session, organization_id=ctx.organization_id
    )
    wanted_id = _as_uuid(identifier)
    lowered = identifier.lower()
    for v in units:
        if wanted_id is not None and v.unit.id == wanted_id:
            return v
        if v.unit.name.lower() == lowered:
            return v
    partial = [v for v in units if lowered in v.unit.name.lower()]
    if len(partial) == 1:
        return partial[0]
    raise ToolError(
        f"no org unit matches {identifier!r} "
        f"(use list_org_units to see the org chart)"
    )


# ---------------------------------------------------------------------------
# search_knowledge — semantic RAG over accessible spaces
# ---------------------------------------------------------------------------


class SearchKnowledgeTool(Tool):
    name = "search_knowledge"
    description = (
        "Semantic search across the documents in every knowledge space the "
        "current user can access (company docs, wikis, synced Feishu files, "
        "uploads). Use this for any question about *content* — policies, "
        "how-tos, meeting notes, specs, reports. Returns the most relevant "
        "text passages with their source document ids and a relevance score. "
        "Not for questions about people, org structure, or access — use the "
        "org tools for those."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The natural-language search query.",
            },
            "top_k": {
                "type": "integer",
                "description": "How many passages to return (1-15, default 6).",
                "minimum": 1,
                "maximum": 15,
            },
        },
        "required": ["query"],
    }

    async def run(self, ctx: ToolContext, args: dict) -> Any:
        query = (args.get("query") or "").strip()
        if not query:
            raise ToolError("query is required")
        top_k = int(args.get("top_k") or 6)
        top_k = max(1, min(top_k, 15))

        qdrant = ctx.require_qdrant()
        result = await retrieve_with_scope(
            ctx.session,
            qdrant=qdrant,
            organization_id=ctx.organization_id,
            caller_user_id=ctx.user_id,
            query=query,
            scope_mode=ctx.knowledge_scope_mode,
            scope_space_ids=ctx.knowledge_space_ids or [],
            top_k=top_k,
        )
        passages = [
            {
                "document_id": str(c.document_id),
                "space_id": str(c.space_id),
                "chunk_index": c.chunk_index,
                "text": c.text,
                "score": round(float(c.score), 4),
            }
            for c in result.chunks
        ]
        return {
            "query": query,
            "accessible_space_count": len(result.space_ids),
            "passage_count": len(passages),
            "passages": passages,
        }


# ---------------------------------------------------------------------------
# list_members — everyone in the org, with dept / position / role
# ---------------------------------------------------------------------------


def _member_to_dict(view: members_service.MemberView) -> dict:
    return {
        "user_id": str(view.user.id),
        "display_name": view.user.display_name,
        "email": view.user.email,
        "status": view.user.status,
        "org_role": view.role_code or None,
        "primary_department": view.primary_org_unit_name,
        "primary_position": view.primary_position_name,
        "direct_managers": [m.display_name for m in view.direct_managers],
    }


class ListMembersTool(Tool):
    name = "list_members"
    description = (
        "List all people in the organization with their primary department, "
        "primary position/title, org role, and direct managers. Use this to "
        "count staff, find who is in a role, or get an overview of the team. "
        "For a single person's full detail use get_member_profile."
    )

    async def run(self, ctx: ToolContext, args: dict) -> Any:
        views = await members_service.list_members(
            ctx.session, organization_id=ctx.organization_id
        )
        return {
            "total": len(views),
            "members": [_member_to_dict(v) for v in views],
        }


# ---------------------------------------------------------------------------
# get_member_profile — one person, enriched
# ---------------------------------------------------------------------------


class GetMemberProfileTool(Tool):
    name = "get_member_profile"
    description = (
        "Get the full profile of one person by name or email: their "
        "department(s), all positions/titles they hold, org role, and who "
        "they report to. Use after list_members when you need detail on a "
        "specific individual."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name_or_email": {
                "type": "string",
                "description": "Full or partial display name, or email address.",
            }
        },
        "required": ["name_or_email"],
    }

    async def run(self, ctx: ToolContext, args: dict) -> Any:
        needle = (args.get("name_or_email") or "").strip().lower()
        if not needle:
            raise ToolError("name_or_email is required")

        views = await members_service.list_members(
            ctx.session, organization_id=ctx.organization_id
        )
        matches = [
            v
            for v in views
            if needle == v.user.email.lower()
            or needle == v.user.display_name.lower()
            or needle in v.user.display_name.lower()
        ]
        if not matches:
            raise ToolError(f"no member matches {args.get('name_or_email')!r}")
        if len(matches) > 1:
            # Ambiguous — hand the model the candidates so it can ask or pick.
            return {
                "ambiguous": True,
                "candidates": [_member_to_dict(v) for v in matches],
            }

        view = matches[0]
        user_id = view.user.id

        positions = await positions_service.list_positions_for_user(
            ctx.session, user_id=user_id, organization_id=ctx.organization_id
        )
        managers = await positions_service.list_reporting_line(
            ctx.session, user_id=user_id, organization_id=ctx.organization_id
        )
        units = await org_units_service.list_units_for_user(
            ctx.session, user_id=user_id, organization_id=ctx.organization_id
        )
        profile = _member_to_dict(view)
        profile.update(
            {
                "positions": [
                    {
                        "position_id": str(pos.id),
                        "name": pos.name,
                        "is_primary": link.is_primary,
                    }
                    for pos, link in positions
                ],
                "reports_to": [u.display_name for u in managers],
                "department_membership_count": len(units),
            }
        )
        return profile


# ---------------------------------------------------------------------------
# list_spaces — knowledge spaces the caller can access
# ---------------------------------------------------------------------------


class ListSpacesTool(Tool):
    name = "list_spaces"
    description = (
        "List the knowledge spaces the current user can access, with each "
        "space's name, purpose/description, and whether the user owns it. "
        "Use this to see what knowledge areas exist before searching, or to "
        "answer questions about which spaces exist or who owns them."
    )

    async def run(self, ctx: ToolContext, args: dict) -> Any:
        views = await spaces_service.list_spaces_for_user(
            ctx.session,
            organization_id=ctx.organization_id,
            user_id=ctx.user_id,
        )
        return {
            "total": len(views),
            "spaces": [
                {
                    "space_id": str(v.space.id),
                    "name": v.space.name,
                    "slug": v.space.slug,
                    "description": v.space.description,
                    "preset": v.space.preset,
                    "visibility": v.space.visibility,
                    "is_owner": v.is_owner,
                }
                for v in views
            ],
        }


# ---------------------------------------------------------------------------
# get_space_access — who has effective access to a space
# ---------------------------------------------------------------------------


class GetSpaceAccessTool(Tool):
    name = "get_space_access"
    description = (
        "List everyone who has effective access to a given knowledge space "
        "(by space name or id), including their effective role and *why* "
        "they have it (direct grant, via a position, a department, or a "
        "group). Use to answer 'who can see space X?' or 'why does person Y "
        "have access?'. Only works for spaces the current user can access."
    )
    parameters = {
        "type": "object",
        "properties": {
            "space": {
                "type": "string",
                "description": "Space name, slug, or id.",
            }
        },
        "required": ["space"],
    }

    async def run(self, ctx: ToolContext, args: dict) -> Any:
        space_view = await _resolve_accessible_space(ctx, args.get("space", ""))
        members = await list_effective_space_members(
            ctx.session,
            organization_id=ctx.organization_id,
            space_id=space_view.space.id,
        )
        return {
            "space_id": str(space_view.space.id),
            "space_name": space_view.space.name,
            "member_count": len(members),
            "members": [
                {
                    "display_name": m.display_name,
                    "email": m.email,
                    "effective_role": m.effective_role,
                    "reasons": [
                        {
                            "via": hp.hit_path,
                            "role": hp.role_code,
                            "subject": hp.subject_display_name or "(self)",
                        }
                        for hp in m.hit_paths
                    ],
                }
                for m in members
            ],
        }


# ---------------------------------------------------------------------------
# list_org_units — the org chart
# ---------------------------------------------------------------------------


class ListOrgUnitsTool(Tool):
    name = "list_org_units"
    description = (
        "List the organization's departments/units as a tree (each unit's "
        "name, id, and parent). Use to understand org structure or to find "
        "the id/name of a department before calling list_org_unit_members."
    )

    async def run(self, ctx: ToolContext, args: dict) -> Any:
        units = await org_units_service.list_tree(
            ctx.session, organization_id=ctx.organization_id
        )
        return {
            "total": len(units),
            "units": [
                {
                    "unit_id": str(v.unit.id),
                    "name": v.unit.name,
                    "parent_id": str(v.unit.parent_id) if v.unit.parent_id else None,
                    "depth": len(v.ancestor_ids),
                }
                for v in units
            ],
        }


# ---------------------------------------------------------------------------
# list_org_unit_members — people in a department
# ---------------------------------------------------------------------------


class ListOrgUnitMembersTool(Tool):
    name = "list_org_unit_members"
    description = (
        "List the people who belong to a specific department/unit (by name "
        "or id). Use to count a department's headcount or to see who is in "
        "it. Note: this returns direct members of that unit only, not "
        "sub-departments."
    )
    parameters = {
        "type": "object",
        "properties": {
            "unit": {
                "type": "string",
                "description": "Department/unit name or id.",
            }
        },
        "required": ["unit"],
    }

    async def run(self, ctx: ToolContext, args: dict) -> Any:
        unit_view = await _resolve_org_unit(ctx, args.get("unit", ""))
        members = await org_units_service.list_members_of_unit(
            ctx.session,
            unit_id=unit_view.unit.id,
            organization_id=ctx.organization_id,
        )
        return {
            "unit_id": str(unit_view.unit.id),
            "unit_name": unit_view.unit.name,
            "member_count": len(members),
            "members": [
                {
                    "display_name": m.user.display_name,
                    "email": m.user.email,
                    "is_primary": m.is_primary,
                }
                for m in members
            ],
        }


# ---------------------------------------------------------------------------
# list_positions — positions with occupancy
# ---------------------------------------------------------------------------


class ListPositionsTool(Tool):
    name = "list_positions"
    description = (
        "List the organization's defined positions/titles with the current "
        "number of people occupying each. Use to answer questions about "
        "roles, headcount budgets, or vacancies."
    )

    async def run(self, ctx: ToolContext, args: dict) -> Any:
        views = await positions_service.list_positions(
            ctx.session, organization_id=ctx.organization_id
        )
        return {
            "total": len(views),
            "positions": [
                {
                    "position_id": str(v.position.id),
                    "name": v.position.name,
                    "current_occupants": v.current_occupants,
                }
                for v in views
            ],
        }


# ---------------------------------------------------------------------------
# Registry factory
# ---------------------------------------------------------------------------


# The full catalogue of built-in tool classes, in the order they should
# appear in the config UI. Adding a tool here makes it available to every
# agent config surface + the runner.
_TOOL_CLASSES: list[type[Tool]] = [
    SearchKnowledgeTool,
    ListMembersTool,
    GetMemberProfileTool,
    ListSpacesTool,
    GetSpaceAccessTool,
    ListOrgUnitsTool,
    ListOrgUnitMembersTool,
    ListPositionsTool,
]

# name -> class, for O(1) assembly by name.
_TOOL_BY_NAME: dict[str, type[Tool]] = {cls.name: cls for cls in _TOOL_CLASSES}

# Ordered list of every built-in tool name.
ALL_TOOL_NAMES: list[str] = [cls.name for cls in _TOOL_CLASSES]


def build_default_tools() -> list[Tool]:
    """Every built-in tool — the generalist agent's full set."""

    return [cls() for cls in _TOOL_CLASSES]


def build_tools_by_names(names: list[str]) -> list[Tool]:
    """Instantiate the subset of tools named in ``names``.

    Unknown names are silently skipped so pruning the catalogue never
    breaks a stored agent config. Order follows the catalogue, not the
    input, for a stable schema hand-off to the model. An empty/all-unknown
    input yields the full default set (a misconfigured agent is still
    useful rather than inert).
    """

    wanted = {n for n in names or []}
    picked = [cls() for cls in _TOOL_CLASSES if cls.name in wanted]
    return picked or build_default_tools()


def tool_catalog() -> list[dict]:
    """Metadata for the config UI: ``[{name, description}]`` per tool."""

    return [
        {"name": cls.name, "description": cls.description.strip()}
        for cls in _TOOL_CLASSES
    ]


# ``NotFoundError`` from a service becomes a recoverable ToolError so the
# model can adjust rather than aborting the whole run.
def normalize_service_error(exc: Exception) -> Optional[ToolError]:
    if isinstance(exc, NotFoundError):
        return ToolError(str(exc))
    return None
