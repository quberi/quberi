"""Private (per-user) AI provider bindings — "bring your own key, just for
me". Any member can register one, regardless of org tier or role; the
only gate is the org's ``allow_byok`` toggle (set by an owner/admin in the
admin console).

These bindings are **never** visible to anyone but the owning user — not
listed in ``GET /organizations/current/ai-providers``, not reachable by
any org-admin endpoint. They do show up (flagged ``is_private``) in that
same user's own ``for_me=true`` usable-chat-models list, so they can be
picked as a per-conversation model override alongside the org default.

Endpoints
---------
* GET    /me/ai-providers              — list the caller's own private bindings
* POST   /me/ai-providers              — add one (rejected if org.allow_byok is off)
* PATCH  /me/ai-providers/{id}         — edit one of the caller's own bindings
* DELETE /me/ai-providers/{id}         — remove one
* POST   /me/ai-providers/probe-models — fetch /v1/models for a not-yet-saved
                                          endpoint or an existing private binding
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.domain.identity.models import Organization
from quberi_api.services import ai_providers as ai_providers_service

router = APIRouter(prefix="/me", tags=["ai-providers"])


class PrivateProviderBindingView(BaseModel):
    id: uuid.UUID
    provider_kind: str
    display_name: str
    base_url: Optional[str] = None
    api_key_present: bool
    status: str
    models: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class PrivateProviderBindingList(BaseModel):
    items: list[PrivateProviderBindingView]


class CreatePrivateProviderBindingInput(BaseModel):
    provider_kind: str = Field(..., description="e.g. openai_compat, dashscope")
    display_name: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    models: Optional[list[str]] = None


class UpdatePrivateProviderBindingInput(BaseModel):
    display_name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    models: Optional[list[str]] = None


class ProbePrivateModelsInput(BaseModel):
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    binding_id: Optional[uuid.UUID] = None


class ProbeModelsResponse(BaseModel):
    models: list[str]


def _view(v: ai_providers_service.ProviderBindingView) -> PrivateProviderBindingView:
    return PrivateProviderBindingView(
        id=v.id,
        provider_kind=v.provider_kind,
        display_name=v.display_name,
        base_url=v.base_url,
        api_key_present=v.api_key_present,
        status=v.status,
        models=v.models,
        created_at=v.created_at,
        updated_at=v.updated_at,
    )


async def _require_byok_allowed(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> None:
    org = (
        await session.execute(
            select(Organization).where(Organization.id == organization_id)
        )
    ).scalar_one()
    await session.commit()
    if not org.allow_byok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="your organization has turned off private (BYOK) models",
        )


@router.get("/ai-providers", response_model=PrivateProviderBindingList)
async def list_my_providers(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> PrivateProviderBindingList:
    views = await ai_providers_service.list_private_bindings_for_identity(
        session, identity_id=caller.user.identity_id
    )
    return PrivateProviderBindingList(items=[_view(v) for v in views])


@router.post(
    "/ai-providers",
    status_code=status.HTTP_201_CREATED,
    response_model=PrivateProviderBindingView,
)
async def create_my_provider(
    payload: CreatePrivateProviderBindingInput,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> PrivateProviderBindingView:
    if caller.is_guest:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="guests cannot add private models",
        )
    await _require_byok_allowed(session, organization_id=caller.organization.id)
    view = await ai_providers_service.register_private_binding(
        session,
        identity_id=caller.user.identity_id,
        provider_kind=payload.provider_kind,
        display_name=payload.display_name,
        base_url=payload.base_url,
        api_key=payload.api_key,
        models=payload.models,
        created_by_user_id=caller.user.id,
    )
    return _view(view)


@router.post(
    "/ai-providers/probe-models",
    response_model=ProbeModelsResponse,
    summary="Fetch the OpenAI-compatible /v1/models list for a private endpoint",
)
async def probe_my_models(
    payload: ProbePrivateModelsInput,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ProbeModelsResponse:
    try:
        if payload.binding_id is not None:
            models = await ai_providers_service.probe_models_for_private_binding(
                session,
                identity_id=caller.user.identity_id,
                binding_id=payload.binding_id,
            )
        elif payload.base_url:
            models = await ai_providers_service.probe_models(
                base_url=payload.base_url, api_key=payload.api_key
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="provide base_url or binding_id",
            )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — endpoint may not support /models
        import httpx

        detail = "拉取失败：请稍后重试，或手动添加模型 id。"
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            if code in (401, 403):
                detail = f"拉取失败：API key 无效或无权限（HTTP {code}）。请填写有效的 API key，或手动添加模型。"
            elif code == 404:
                detail = "拉取失败：该端点不提供 /v1/models 列表（HTTP 404）。请手动添加模型 id。"
            else:
                detail = f"拉取失败：供应商返回 HTTP {code}。请检查 Base URL 与 API key。"
        elif isinstance(exc, httpx.RequestError):
            detail = "拉取失败：无法连接该 Base URL，请检查地址是否正确。"
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=detail
        ) from exc
    return ProbeModelsResponse(models=models)


@router.patch("/ai-providers/{binding_id}", response_model=PrivateProviderBindingView)
async def update_my_provider(
    binding_id: uuid.UUID,
    payload: UpdatePrivateProviderBindingInput,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> PrivateProviderBindingView:
    fields = payload.model_dump(exclude_unset=True)
    kwargs = {
        k: fields[k]
        for k in ("display_name", "base_url", "api_key", "models")
        if k in fields
    }
    view = await ai_providers_service.update_private_binding(
        session,
        identity_id=caller.user.identity_id,
        binding_id=binding_id,
        **kwargs,
    )
    if view is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="provider not found",
        )
    return _view(view)


@router.delete(
    "/ai-providers/{binding_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_my_provider(
    binding_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await ai_providers_service.delete_private_binding(
        session,
        identity_id=caller.user.identity_id,
        binding_id=binding_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


me_ai_providers_router = router
