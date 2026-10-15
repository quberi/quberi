"""Agent persona configuration service.

CRUD over :class:`Agent` rows plus lazy seeding. The runner consumes
these to assemble a per-agent tool registry + system prompt.

Seeding
-------
On first :func:`list_agents` (or :func:`get_default_agent`) for an org
that has no agents yet, we seed a small starter set: a generalist
(``is_default``, every tool) plus two focused templates. This converges
existing and brand-new orgs to a working state without a data migration.

Permissions
-----------
Reads are open to any member of the org (the caller is already resolved
+ tenant-scoped upstream). Writes (create/update/archive) are gated to
org owner/admin — enforced by the API layer via ``caller_has_org_role``,
mirroring members/positions/groups.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.agents.models import Agent
from quberi_api.domain.enums import AGENT_KNOWLEDGE_SCOPE_MODES, AGENT_VISIBILITY
from quberi_api.domain.spaces.models import Space
from quberi_api.services.agents.tools import ALL_TOOL_NAMES
from quberi_api.services.authz import (
    caller_has_org_role,
    resolve_effective_role_codes,
)
from quberi_api.services.errors import InvalidInputError, NotFoundError
from quberi_api.services.security.crypto import seal

# Agent kinds + the external protocols we support.
#   openai_chat — any OpenAI /chat/completions compatible endpoint
#   coze        — 扣子 Coze /v3/chat (needs a bot_id)
#   dify        — Dify /chat-messages (app key; base_url endpoint)
AGENT_KINDS = ("builtin", "external")
EXTERNAL_PROTOCOLS = ("openai_chat", "coze", "dify")

# Default endpoints so users can leave them blank for the hosted platforms.
_EXTERNAL_DEFAULT_ENDPOINT = {
    "coze": "https://api.coze.cn",
    "dify": "https://api.dify.ai/v1",
}


def _validate_external(
    *,
    protocol: Optional[str],
    endpoint: Optional[str],
    model: Optional[str],
    bot_id: Optional[str],
    extra: Optional[dict],
) -> dict:
    """Validate + normalise an external agent's config into a JSONB blob.

    Required fields vary by protocol:
      * openai_chat — endpoint + model
      * coze        — endpoint (default api.coze.cn) + bot_id
      * dify        — endpoint (base_url, default api.dify.ai/v1)
    """

    proto = (protocol or "openai_chat").strip()
    if proto not in EXTERNAL_PROTOCOLS:
        raise InvalidInputError(
            f"unsupported external agent protocol: {proto!r} "
            f"(allowed: {list(EXTERNAL_PROTOCOLS)})"
        )
    url = (endpoint or "").strip().rstrip("/") or _EXTERNAL_DEFAULT_ENDPOINT.get(proto, "")
    if not url:
        raise InvalidInputError("external agent requires an endpoint URL")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise InvalidInputError("external agent endpoint must be an http(s) URL")

    mdl = (model or "").strip()
    bid = (bot_id or "").strip()
    clean_extra = extra if isinstance(extra, dict) else {}

    if proto == "openai_chat" and not mdl:
        raise InvalidInputError("OpenAI-compatible agent requires a model name")
    if proto == "coze" and not bid:
        raise InvalidInputError("Coze agent requires a bot_id")

    return {
        "protocol": proto,
        "endpoint": url,
        "model": mdl,
        "bot_id": bid,
        "extra": clean_extra,
    }

# ---------------------------------------------------------------------------
# Seed definitions
# ---------------------------------------------------------------------------

# Focused knowledge / org tool groupings, referenced by the templates.
_KNOWLEDGE_TOOLS = ["search_knowledge", "list_spaces", "get_space_access"]
_ORG_TOOLS = [
    "list_members",
    "get_member_profile",
    "list_org_units",
    "list_org_unit_members",
    "list_positions",
]


def _seed_specs() -> list[dict]:
    """The starter agents seeded for a fresh org.

    Chinese labels match the product's primary market; users can rename
    or add their own from the config UI.
    """

    return [
        {
            "name": "通用助手",
            "description": "跨知识库与组织数据的通用智能体，可自主规划并调用全部工具。",
            "icon": "sparkles",
            "system_prompt": "",
            "enabled_tools": list(ALL_TOOL_NAMES),
            "is_default": True,
        },
        {
            "name": "组织架构助手",
            "description": "专注人员、部门、职位与访问权限——回答“谁在哪个团队、谁能访问什么”这类问题。",
            "icon": "users",
            "system_prompt": (
                "你是组织架构方面的专家助手，专注于人员、部门、职位、汇报关系与访问权限相关的问题。"
                "优先使用组织类工具作答，并在有帮助时组合多个来源。"
            ),
            "enabled_tools": list(_ORG_TOOLS),
            "is_default": False,
        },
        {
            "name": "知识检索助手",
            "description": "专注在知识库中检索并总结文档内容——政策、手册、会议记录、规范等。",
            "icon": "book",
            "system_prompt": (
                "你是知识检索方面的专家助手，专注于在知识库中查找并归纳文档内容。"
                "先用 search_knowledge 检索，再基于命中的段落作答，不要编造文档内容。"
            ),
            "enabled_tools": list(_KNOWLEDGE_TOOLS),
            "is_default": False,
        },
    ]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_name(name: str) -> str:
    trimmed = (name or "").strip()
    if not trimmed:
        raise InvalidInputError("agent name is required")
    if len(trimmed) > 200:
        raise InvalidInputError("agent name exceeds 200 characters")
    return trimmed


def _validate_tools(tools: Optional[list]) -> list[str]:
    if not tools:
        raise InvalidInputError("an agent must enable at least one tool")
    unknown = [t for t in tools if t not in ALL_TOOL_NAMES]
    if unknown:
        raise InvalidInputError(f"unknown tool(s): {', '.join(map(str, unknown))}")
    # De-dupe while preserving catalogue order.
    wanted = set(tools)
    return [name for name in ALL_TOOL_NAMES if name in wanted]


def _validate_icon(icon: Optional[str]) -> str:
    trimmed = (icon or "").strip() or "bot"
    return trimmed[:48]


def _validate_scope_mode(mode: Optional[str]) -> str:
    mode = (mode or "user").strip()
    if mode not in AGENT_KNOWLEDGE_SCOPE_MODES:
        raise InvalidInputError(
            f"unknown knowledge_scope_mode: {mode!r} "
            f"(allowed: {list(AGENT_KNOWLEDGE_SCOPE_MODES)})"
        )
    return mode


def _validate_visibility(visibility: Optional[str]) -> str:
    visibility = (visibility or "organization").strip()
    if visibility not in AGENT_VISIBILITY:
        raise InvalidInputError(
            f"unknown visibility: {visibility!r} (allowed: {list(AGENT_VISIBILITY)})"
        )
    return visibility


async def _validate_scope_spaces(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    scope_mode: str,
    space_ids: Optional[list],
) -> list[str]:
    """Normalise + validate ``scope_space_ids`` for the chosen mode.

    For ``user`` mode the list is meaningless → stored empty. For
    ``intersect`` / ``fixed`` the list is required and every id must be a
    space in this organisation.
    """

    if scope_mode == "user":
        return []
    raw = space_ids or []
    if not raw:
        raise InvalidInputError(
            "intersect/fixed knowledge scope requires at least one space"
        )
    # Coerce to UUID + de-dupe.
    wanted: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for sid in raw:
        try:
            u = uuid.UUID(str(sid))
        except (ValueError, TypeError) as exc:
            raise InvalidInputError(f"invalid space id: {sid!r}") from exc
        if u not in seen:
            seen.add(u)
            wanted.append(u)
    rows = (
        await session.execute(
            select(Space.id).where(
                Space.organization_id == organization_id,
                Space.id.in_(wanted),
            )
        )
    ).scalars().all()
    found = set(rows)
    missing = [str(u) for u in wanted if u not in found]
    if missing:
        raise InvalidInputError(
            f"space(s) not found in this organisation: {', '.join(missing)}"
        )
    return [str(u) for u in wanted]


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


async def ensure_seeded(session: AsyncSession, *, organization_id: uuid.UUID) -> None:
    """Seed the starter agents if the org has none. Idempotent."""

    existing = (
        await session.execute(
            select(Agent.id)
            .where(Agent.organization_id == organization_id)
            .limit(1)
        )
    ).first()
    if existing is not None:
        return

    # The existence SELECT auto-began a read transaction; close it before
    # opening our own write transaction (else "already begun" fires).
    await session.commit()
    async with session.begin():
        # Re-check inside the transaction to avoid a double-seed race.
        again = (
            await session.execute(
                select(Agent.id)
                .where(Agent.organization_id == organization_id)
                .limit(1)
            )
        ).first()
        if again is not None:
            return
        for spec in _seed_specs():
            session.add(
                Agent(
                    organization_id=organization_id,
                    name=spec["name"],
                    description=spec["description"],
                    icon=spec["icon"],
                    system_prompt=spec["system_prompt"],
                    enabled_tools=spec["enabled_tools"],
                    is_default=spec["is_default"],
                    status="active",
                )
            )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_agents(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    caller_user_id: uuid.UUID,
) -> list[Agent]:
    """Agents the caller may see (default first, then oldest). Seeds on demand.

    Visibility filter: an ``organization`` agent is visible to everyone;
    a ``restricted`` agent only to subjects granted access on it — plus
    org owners/admins, who manage the whole roster.
    """

    await ensure_seeded(session, organization_id=organization_id)
    stmt = (
        select(Agent)
        .where(Agent.organization_id == organization_id)
        .where(Agent.status == "active")
        .order_by(Agent.is_default.desc(), Agent.created_at.asc())
    )
    agents = list((await session.execute(stmt)).scalars().all())

    # Fast path: if there are no restricted agents, skip the per-agent
    # authorization walk entirely.
    if all(a.visibility == "organization" for a in agents):
        return agents

    is_admin = await caller_has_org_role(
        session,
        organization_id=organization_id,
        user_id=caller_user_id,
        allowed_codes=("owner", "admin"),
    )
    if is_admin:
        return agents

    visible: list[Agent] = []
    for agent in agents:
        if agent.visibility == "organization":
            visible.append(agent)
            continue
        codes = await resolve_effective_role_codes(
            session,
            user_id=caller_user_id,
            organization_id=organization_id,
            resource_type="agent",
            resource_id=agent.id,
        )
        if codes:
            visible.append(agent)
    return visible


async def can_use_agent(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    agent: Agent,
) -> bool:
    """Whether ``user`` may see + run ``agent`` (visibility check)."""

    if agent.visibility == "organization":
        return True
    is_admin = await caller_has_org_role(
        session,
        organization_id=organization_id,
        user_id=user_id,
        allowed_codes=("owner", "admin"),
    )
    if is_admin:
        return True
    codes = await resolve_effective_role_codes(
        session,
        user_id=user_id,
        organization_id=organization_id,
        resource_type="agent",
        resource_id=agent.id,
    )
    return bool(codes)


async def get_agent(
    session: AsyncSession, *, organization_id: uuid.UUID, agent_id: uuid.UUID
) -> Agent:
    stmt = select(Agent).where(
        Agent.id == agent_id,
        Agent.organization_id == organization_id,
        Agent.status == "active",
    )
    agent = (await session.execute(stmt)).scalar_one_or_none()
    if agent is None:
        raise NotFoundError(f"agent not found: {agent_id}")
    return agent


async def get_default_agent(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> Agent:
    """The org's seeded generalist (seeds first if needed)."""

    await ensure_seeded(session, organization_id=organization_id)
    stmt = (
        select(Agent)
        .where(Agent.organization_id == organization_id)
        .where(Agent.status == "active")
        .where(Agent.is_default.is_(True))
        .limit(1)
    )
    agent = (await session.execute(stmt)).scalar_one_or_none()
    if agent is None:
        # No default flag set (edge case) — fall back to any active agent.
        stmt2 = (
            select(Agent)
            .where(Agent.organization_id == organization_id)
            .where(Agent.status == "active")
            .order_by(Agent.created_at.asc())
            .limit(1)
        )
        agent = (await session.execute(stmt2)).scalar_one_or_none()
    if agent is None:
        raise NotFoundError("no agent available for organisation")
    return agent


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


async def create_agent(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    name: str,
    description: Optional[str],
    icon: Optional[str],
    system_prompt: Optional[str],
    enabled_tools: list[str],
    knowledge_scope_mode: Optional[str] = None,
    scope_space_ids: Optional[list] = None,
    visibility: Optional[str] = None,
    kind: Optional[str] = None,
    external_protocol: Optional[str] = None,
    external_endpoint: Optional[str] = None,
    external_model: Optional[str] = None,
    external_bot_id: Optional[str] = None,
    external_extra: Optional[dict] = None,
    external_api_key: Optional[str] = None,
) -> Agent:
    name = _validate_name(name)
    icon = _validate_icon(icon)
    desc = (description or "").strip() or None
    prompt = (system_prompt or "").strip()
    vis = _validate_visibility(visibility)

    agent_kind = (kind or "builtin").strip()
    if agent_kind not in AGENT_KINDS:
        raise InvalidInputError(
            f"unknown agent kind: {agent_kind!r} (allowed: {list(AGENT_KINDS)})"
        )

    external_config: dict = {}
    if agent_kind == "external":
        # External agents forward to a third-party endpoint; they don't run
        # our tool loop, so tools/knowledge-scope are irrelevant.
        tools = []
        scope_mode = "user"
        spaces = []
        external_config = _validate_external(
            protocol=external_protocol,
            endpoint=external_endpoint,
            model=external_model,
            bot_id=external_bot_id,
            extra=external_extra,
        )
    else:
        tools = _validate_tools(enabled_tools)
        scope_mode = _validate_scope_mode(knowledge_scope_mode)
        spaces = await _validate_scope_spaces(
            session,
            organization_id=organization_id,
            scope_mode=scope_mode,
            space_ids=scope_space_ids,
        )
    # The scope-space validation ran a SELECT (auto-begun read txn); close
    # it before opening the write transaction.
    await session.commit()

    async with session.begin():
        agent = Agent(
            organization_id=organization_id,
            name=name,
            description=desc,
            icon=icon,
            system_prompt=prompt,
            enabled_tools=tools,
            knowledge_scope_mode=scope_mode,
            scope_space_ids=spaces,
            visibility=vis,
            kind=agent_kind,
            external_config=external_config,
            external_api_key_encrypted=(
                seal(external_api_key)
                if (agent_kind == "external" and external_api_key)
                else None
            ),
            is_default=False,
            status="active",
            created_by_user_id=actor_user_id,
        )
        session.add(agent)
        await session.flush()
        await session.refresh(agent, attribute_names=["created_at", "updated_at"])
    return agent


async def update_agent(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    agent_id: uuid.UUID,
    name: Optional[str] = None,
    description: Optional[str] = None,
    icon: Optional[str] = None,
    system_prompt: Optional[str] = None,
    enabled_tools: Optional[list[str]] = None,
    knowledge_scope_mode: Optional[str] = None,
    scope_space_ids: Optional[list] = None,
    visibility: Optional[str] = None,
    kind: Optional[str] = None,
    external_protocol: Optional[str] = None,
    external_endpoint: Optional[str] = None,
    external_model: Optional[str] = None,
    external_bot_id: Optional[str] = None,
    external_extra: Optional[dict] = None,
    external_api_key: Optional[str] = None,
) -> Agent:
    """Patch an agent. Only provided fields change. ``is_default`` is fixed."""

    agent = await get_agent(
        session, organization_id=organization_id, agent_id=agent_id
    )

    # Pre-validate everything (some validators run their own SELECTs) while
    # still inside the read txn, then commit before the write.
    updates: dict = {}
    if name is not None:
        updates["name"] = _validate_name(name)
    if description is not None:
        updates["description"] = description.strip() or None
    if icon is not None:
        updates["icon"] = _validate_icon(icon)
    if system_prompt is not None:
        updates["system_prompt"] = system_prompt.strip()
    if enabled_tools is not None:
        updates["enabled_tools"] = _validate_tools(enabled_tools)
    if visibility is not None:
        updates["visibility"] = _validate_visibility(visibility)

    # Kind + external config. When the effective kind is ``external`` we
    # re-validate the full config, merging provided fields over current.
    effective_kind = agent.kind
    if kind is not None:
        if kind not in AGENT_KINDS:
            raise InvalidInputError(
                f"unknown agent kind: {kind!r} (allowed: {list(AGENT_KINDS)})"
            )
        updates["kind"] = kind
        effective_kind = kind
    external_touched = any(
        v is not None
        for v in (
            external_protocol,
            external_endpoint,
            external_model,
            external_bot_id,
            external_extra,
        )
    )
    if effective_kind == "external" and (kind is not None or external_touched):
        cfg = agent.external_config or {}
        updates["external_config"] = _validate_external(
            protocol=external_protocol
            if external_protocol is not None
            else cfg.get("protocol"),
            endpoint=external_endpoint
            if external_endpoint is not None
            else cfg.get("endpoint"),
            model=external_model if external_model is not None else cfg.get("model"),
            bot_id=external_bot_id
            if external_bot_id is not None
            else cfg.get("bot_id"),
            extra=external_extra if external_extra is not None else cfg.get("extra"),
        )
    if external_api_key:
        updates["external_api_key_encrypted"] = seal(external_api_key)

    # Knowledge scope: mode + spaces are validated together so the space
    # list matches the (possibly new) mode. If only one is supplied we fall
    # back to the agent's current value for the other.
    if knowledge_scope_mode is not None or scope_space_ids is not None:
        effective_mode = _validate_scope_mode(
            knowledge_scope_mode
            if knowledge_scope_mode is not None
            else agent.knowledge_scope_mode
        )
        effective_spaces = (
            scope_space_ids
            if scope_space_ids is not None
            else agent.scope_space_ids
        )
        updates["knowledge_scope_mode"] = effective_mode
        updates["scope_space_ids"] = await _validate_scope_spaces(
            session,
            organization_id=organization_id,
            scope_mode=effective_mode,
            space_ids=effective_spaces,
        )

    await session.commit()

    async with session.begin():
        agent = await session.get(Agent, agent_id)
        if agent is None or agent.organization_id != organization_id:
            raise NotFoundError(f"agent not found: {agent_id}")
        for field, value in updates.items():
            setattr(agent, field, value)
        await session.flush()
        await session.refresh(agent, attribute_names=["updated_at"])
    return agent


async def archive_agent(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> None:
    """Soft-delete an agent. The default generalist cannot be archived."""

    agent = await get_agent(
        session, organization_id=organization_id, agent_id=agent_id
    )
    if agent.is_default:
        raise InvalidInputError("the default agent cannot be deleted")
    await session.commit()

    async with session.begin():
        agent = await session.get(Agent, agent_id)
        if agent is None or agent.organization_id != organization_id:
            raise NotFoundError(f"agent not found: {agent_id}")
        agent.status = "archived"
        await session.flush()
