"""Agent endpoints.

Config
------
* GET    /api/v1/agents             — list the org's agent personas
* GET    /api/v1/agents/tools       — catalogue of available tools
* POST   /api/v1/agents             — create a persona           (admin)
* GET    /api/v1/agents/{id}        — one persona's full config
* PATCH  /api/v1/agents/{id}        — update a persona           (admin)
* DELETE /api/v1/agents/{id}        — archive a persona          (admin)

Run
---
* POST   /api/v1/agents/{id}/run         — run to completion (answer+trace)
* POST   /api/v1/agents/{id}/run/stream  — SSE reasoning + answer

Reads are open to any member of the tenant; config writes are gated to
org owner/admin. The runner scopes every tool to the calling user, so an
agent can never reach beyond what that user could see by hand.

Route ordering note: the static ``/tools`` route is declared before
``/{agent_id}`` so "tools" is never parsed as a UUID.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.api.v1.agents.schemas import (
    AgentAccessGrant,
    AgentAccessList,
    AgentDetail,
    AgentList,
    AgentSummary,
    CreateAgentRequest,
    GrantAgentAccessRequest,
    RunAgentRequest,
    RunAgentResponse,
    ToolCatalog,
    ToolInfo,
    UpdateAgentRequest,
)
from quberi_api.services.agents import access as agents_access
from quberi_api.services.agents import config as agents_config
from quberi_api.services.agents.runner import run_agent, run_agent_stream
from quberi_api.services import usage as usage_service
from quberi_api.services.agents.tools import tool_catalog
from quberi_api.services.authz import caller_has_org_role
from quberi_api.services.errors import (
    AlreadyExistsError,
    InvalidInputError,
    NotFoundError,
)

agents_router = APIRouter(prefix="/agents", tags=["agents"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _require_org_admin(caller: CallerContext, session: AsyncSession) -> None:
    allowed = await caller_has_org_role(
        session,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
        allowed_codes=("owner", "admin"),
    )
    # Release the auto-began read transaction so the downstream service
    # (which owns its own ``session.begin()``) starts from a clean slate.
    await session.commit()
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only organisation owners or admins can manage agents",
        )


def _external_fields(agent) -> dict:
    cfg = getattr(agent, "external_config", None) or {}
    return {
        "kind": getattr(agent, "kind", "builtin") or "builtin",
        "external_protocol": cfg.get("protocol"),
        "external_endpoint": cfg.get("endpoint"),
        "external_model": cfg.get("model"),
        "external_bot_id": cfg.get("bot_id"),
        "external_api_key_present": bool(
            getattr(agent, "external_api_key_encrypted", None)
        ),
    }


def _summary(agent) -> AgentSummary:
    return AgentSummary.model_validate(agent).model_copy(update=_external_fields(agent))


def _detail(agent) -> AgentDetail:
    return AgentDetail.model_validate(agent).model_copy(update=_external_fields(agent))


async def _resolve_usable_agent(
    caller: CallerContext,
    session: AsyncSession,
    agent_id: uuid.UUID,
):
    """Load an agent the caller is allowed to see/run, or raise 404.

    Restricted agents the caller has no grant on look non-existent (we
    return 404 rather than 403 so visibility itself isn't leaked).
    """

    try:
        agent = await agents_config.get_agent(
            session, organization_id=caller.organization.id, agent_id=agent_id
        )
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    allowed = await agents_config.can_use_agent(
        session,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
        agent=agent,
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"agent not found: {agent_id}"
        )
    return agent


# ---------------------------------------------------------------------------
# Config — list / catalogue (static routes first)
# ---------------------------------------------------------------------------


@agents_router.get("", response_model=AgentList, summary="List agent personas")
async def list_agents(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AgentList:
    agents = await agents_config.list_agents(
        session,
        organization_id=caller.organization.id,
        caller_user_id=caller.user.id,
    )
    items = [_summary(a) for a in agents]
    return AgentList(items=items, total=len(items))


@agents_router.get(
    "/tools",
    response_model=ToolCatalog,
    summary="List the built-in tools an agent can be given",
)
async def list_tools(
    caller: CallerContext = Depends(require_caller),
) -> ToolCatalog:
    return ToolCatalog(items=[ToolInfo(**t) for t in tool_catalog()])


@agents_router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=AgentDetail,
    summary="Create an agent persona",
)
async def create_agent(
    payload: CreateAgentRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AgentDetail:
    await _require_org_admin(caller, session)
    try:
        agent = await agents_config.create_agent(
            session,
            organization_id=caller.organization.id,
            actor_user_id=caller.user.id,
            name=payload.name,
            description=payload.description,
            icon=payload.icon,
            system_prompt=payload.system_prompt,
            enabled_tools=payload.enabled_tools,
            knowledge_scope_mode=payload.knowledge_scope_mode,
            scope_space_ids=payload.scope_space_ids,
            visibility=payload.visibility,
            kind=payload.kind,
            external_protocol=payload.external_protocol,
            external_endpoint=payload.external_endpoint,
            external_model=payload.external_model,
            external_bot_id=payload.external_bot_id,
            external_extra=payload.external_extra,
            external_api_key=payload.external_api_key,
        )
    except InvalidInputError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return _detail(agent)


@agents_router.get(
    "/{agent_id}",
    response_model=AgentDetail,
    summary="Get one agent persona's full config",
)
async def get_agent(
    agent_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AgentDetail:
    agent = await _resolve_usable_agent(caller, session, agent_id)
    return _detail(agent)


@agents_router.patch(
    "/{agent_id}",
    response_model=AgentDetail,
    summary="Update an agent persona",
)
async def update_agent(
    agent_id: uuid.UUID,
    payload: UpdateAgentRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AgentDetail:
    await _require_org_admin(caller, session)
    try:
        agent = await agents_config.update_agent(
            session,
            organization_id=caller.organization.id,
            agent_id=agent_id,
            name=payload.name,
            description=payload.description,
            icon=payload.icon,
            system_prompt=payload.system_prompt,
            enabled_tools=payload.enabled_tools,
            knowledge_scope_mode=payload.knowledge_scope_mode,
            scope_space_ids=payload.scope_space_ids,
            visibility=payload.visibility,
            kind=payload.kind,
            external_protocol=payload.external_protocol,
            external_endpoint=payload.external_endpoint,
            external_model=payload.external_model,
            external_bot_id=payload.external_bot_id,
            external_extra=payload.external_extra,
            external_api_key=payload.external_api_key,
        )
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except InvalidInputError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return _detail(agent)


@agents_router.delete(
    "/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Archive an agent persona",
)
async def delete_agent(
    agent_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await _require_org_admin(caller, session)
    try:
        await agents_config.archive_agent(
            session, organization_id=caller.organization.id, agent_id=agent_id
        )
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except InvalidInputError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


@agents_router.post(
    "/{agent_id}/run",
    response_model=RunAgentResponse,
    summary="Run an agent and return the final answer plus reasoning trace",
)
async def run(
    agent_id: uuid.UUID,
    payload: RunAgentRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> RunAgentResponse:
    # Visibility gate (404 for agents the caller can't use).
    await _resolve_usable_agent(caller, session, agent_id)
    # Free-quota gate (402 when a platform-key org is out of allowance).
    await usage_service.enforce_free_quota(
        session,
        organization_id=caller.organization.id,
        tier=caller.organization.tier,
    )
    try:
        result = await run_agent(
            session,
            organization_id=caller.organization.id,
            user_id=caller.user.id,
            query=payload.query,
            agent_id=agent_id,
        )
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return RunAgentResponse(
        answer=result.answer,
        iterations=result.iterations,
        model_id=result.model_id,
        tool_call_count=result.tool_call_count,
        trace=[s.to_dict() for s in result.trace],
    )


@agents_router.post(
    "/{agent_id}/run/stream",
    summary="Run an agent and stream reasoning + answer as SSE",
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": (
                "Server-Sent Events with kinds: tool_call, tool_result, "
                "note, delta, error, done."
            ),
        }
    },
)
async def run_stream(
    agent_id: uuid.UUID,
    payload: RunAgentRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    # Visibility gate before we open the stream (404 as a normal response).
    await _resolve_usable_agent(caller, session, agent_id)
    # Free-quota gate before the stream opens (clean 402 if exhausted).
    await usage_service.enforce_free_quota(
        session,
        organization_id=caller.organization.id,
        tier=caller.organization.tier,
    )

    async def event_source():
        try:
            async for kind, data in run_agent_stream(
                session,
                organization_id=caller.organization.id,
                user_id=caller.user.id,
                query=payload.query,
                agent_id=agent_id,
            ):
                payload_str = json.dumps(data, ensure_ascii=False, default=str)
                yield f"event: {kind}\ndata: {payload_str}\n\n"
        except Exception as exc:  # noqa: BLE001 — surface as SSE error
            msg = f"{type(exc).__name__}: {exc}"
            yield (
                "event: error\n"
                f"data: {json.dumps({'message': msg}, ensure_ascii=False)}\n\n"
            )
            yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Access grants (who may use a restricted agent) — admin only
# ---------------------------------------------------------------------------


def _access_grant(view) -> AgentAccessGrant:
    return AgentAccessGrant(
        id=view.binding.id,
        subject_type=view.subject_type,
        subject_id=view.subject_id,
        subject_display_name=view.subject_display_name,
        subject_subtitle=view.subject_subtitle,
        created_at=view.binding.created_at,
    )


@agents_router.get(
    "/{agent_id}/access",
    response_model=AgentAccessList,
    summary="List the subjects granted access to a restricted agent",
)
async def list_access(
    agent_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AgentAccessList:
    await _require_org_admin(caller, session)
    views = await agents_access.list_agent_access(
        session, organization_id=caller.organization.id, agent_id=agent_id
    )
    items = [_access_grant(v) for v in views]
    return AgentAccessList(items=items, total=len(items))


@agents_router.post(
    "/{agent_id}/access",
    status_code=status.HTTP_201_CREATED,
    response_model=AgentAccessGrant,
    summary="Grant a subject access to a restricted agent",
)
async def grant_access(
    agent_id: uuid.UUID,
    payload: GrantAgentAccessRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AgentAccessGrant:
    await _require_org_admin(caller, session)
    try:
        view = await agents_access.grant_agent_access(
            session,
            organization_id=caller.organization.id,
            agent_id=agent_id,
            actor_user_id=caller.user.id,
            subject_type=payload.subject_type,
            subject_id=payload.subject_id,
        )
    except AlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except (InvalidInputError,) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return _access_grant(view)


@agents_router.delete(
    "/{agent_id}/access/{binding_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Revoke a subject's access to a restricted agent",
)
async def revoke_access(
    agent_id: uuid.UUID,
    binding_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await _require_org_admin(caller, session)
    try:
        await agents_access.revoke_agent_access(
            session,
            organization_id=caller.organization.id,
            agent_id=agent_id,
            binding_id=binding_id,
            actor_user_id=caller.user.id,
        )
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
