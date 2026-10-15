"""AI provider bindings + model selections — REST surface.

The routes are org-scoped through the caller's context (same
``require_caller`` guard as everything else). Platform-default
bindings are read-only through this route; ops rotates them via the
``.env`` file + a restart.

Endpoints
---------
* GET    /organizations/current/ai-providers            — list all bindings visible to org
* POST   /organizations/current/ai-providers            — create an org-scoped binding
* DELETE /organizations/current/ai-providers/{id}       — remove an org binding
* GET    /organizations/current/ai-models               — catalog for the UI
* GET    /organizations/current/ai-model-selections     — current per-purpose choices
* PUT    /organizations/current/ai-model-selections/{p} — set a selection
* DELETE /organizations/current/ai-model-selections/{p} — clear a selection (fall back to platform default)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.services import ai_providers as ai_providers_service
from quberi_api.services.ai.registry import MODEL_REGISTRY

router = APIRouter(prefix="/organizations/current", tags=["ai-providers"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ProviderBindingView(BaseModel):
    id: uuid.UUID
    organization_id: Optional[uuid.UUID]
    provider_kind: str
    vendor: Optional[str] = None
    display_name: str
    base_url: Optional[str] = None
    api_key_present: bool
    status: str
    is_platform_default: bool
    priority: int = 100
    extra_config: dict = Field(default_factory=dict)
    models: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ProviderBindingList(BaseModel):
    items: list[ProviderBindingView]


class CreateProviderBindingInput(BaseModel):
    provider_kind: str = Field(..., description="e.g. openai_compat, openai, dashscope")
    display_name: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    models: Optional[list[str]] = None
    extra_config: Optional[dict] = None


class UpdateProviderBindingInput(BaseModel):
    # All optional; only provided fields are applied. A blank/omitted
    # api_key keeps the existing key (write-only, never returned).
    display_name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    models: Optional[list[str]] = None


class ProbeModelsInput(BaseModel):
    # Fetch /v1/models. Provide base_url + api_key for a not-yet-saved
    # provider, or binding_id to reuse a saved binding's stored key.
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    binding_id: Optional[uuid.UUID] = None


class ProbeModelsResponse(BaseModel):
    models: list[str]


class ModelCapabilityView(BaseModel):
    model_id: str
    provider_kind: str
    purpose: str
    display_name: str
    vector_dim: Optional[int] = None
    context_window: int
    notes: str


class ModelSelectionsView(BaseModel):
    chat: dict
    embedding: dict
    reranker: dict


class SetModelSelectionInput(BaseModel):
    model_id: str
    binding_id: uuid.UUID


# ---------------------------------------------------------------------------
# Provider bindings
# ---------------------------------------------------------------------------


@router.get("/ai-providers", response_model=ProviderBindingList)
async def list_providers(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ProviderBindingList:
    views = await ai_providers_service.list_bindings_for_org(
        session, organization_id=caller.organization.id
    )
    return ProviderBindingList(
        items=[
            ProviderBindingView(
                id=v.id,
                organization_id=v.organization_id,
                provider_kind=v.provider_kind,
                vendor=v.vendor,
                display_name=v.display_name,
                base_url=v.base_url,
                api_key_present=v.api_key_present,
                status=v.status,
                is_platform_default=v.is_platform_default,
                priority=v.priority,
                extra_config=v.extra_config,
                models=v.models,
                created_at=v.created_at,
                updated_at=v.updated_at,
            )
            for v in views
        ]
    )


@router.post(
    "/ai-providers",
    response_model=ProviderBindingView,
    status_code=status.HTTP_201_CREATED,
)
async def create_provider(
    payload: CreateProviderBindingInput,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ProviderBindingView:
    view = await ai_providers_service.register_binding(
        session,
        organization_id=caller.organization.id,
        provider_kind=payload.provider_kind,
        display_name=payload.display_name,
        base_url=payload.base_url,
        api_key=payload.api_key,
        models=payload.models,
        extra_config=payload.extra_config,
        created_by_user_id=caller.user.id,
    )
    return ProviderBindingView(
        id=view.id,
        organization_id=view.organization_id,
        provider_kind=view.provider_kind,
        vendor=view.vendor,
        display_name=view.display_name,
        base_url=view.base_url,
        api_key_present=view.api_key_present,
        status=view.status,
        is_platform_default=view.is_platform_default,
        priority=view.priority,
        extra_config=view.extra_config,
        models=view.models,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


@router.post(
    "/ai-providers/probe-models",
    response_model=ProbeModelsResponse,
    summary="Fetch the OpenAI-compatible /v1/models list for a provider",
)
async def probe_models(
    payload: ProbeModelsInput,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ProbeModelsResponse:
    try:
        if payload.binding_id is not None:
            models = await ai_providers_service.probe_models_for_binding(
                session,
                organization_id=caller.organization.id,
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


@router.patch(
    "/ai-providers/{binding_id}",
    response_model=ProviderBindingView,
)
async def update_provider(
    binding_id: uuid.UUID,
    payload: UpdateProviderBindingInput,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ProviderBindingView:
    fields = payload.model_dump(exclude_unset=True)
    kwargs = {
        k: fields[k]
        for k in ("display_name", "base_url", "api_key", "models")
        if k in fields
    }
    view = await ai_providers_service.update_binding(
        session,
        organization_id=caller.organization.id,
        binding_id=binding_id,
        **kwargs,
    )
    if view is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="provider not found or not editable",
        )
    return ProviderBindingView(
        id=view.id,
        organization_id=view.organization_id,
        provider_kind=view.provider_kind,
        vendor=view.vendor,
        display_name=view.display_name,
        base_url=view.base_url,
        api_key_present=view.api_key_present,
        status=view.status,
        is_platform_default=view.is_platform_default,
        priority=view.priority,
        extra_config=view.extra_config,
        models=view.models,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


@router.delete(
    "/ai-providers/{binding_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_provider(
    binding_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await ai_providers_service.delete_binding(
        session,
        organization_id=caller.organization.id,
        binding_id=binding_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Model catalog + selections
# ---------------------------------------------------------------------------


class UsableChatModelView(BaseModel):
    model_id: str
    display_name: str
    binding_id: uuid.UUID
    binding_name: str
    is_platform_default: bool
    vendor: Optional[str] = None
    is_default: bool = False
    is_private: bool = False


class UsableChatModelList(BaseModel):
    items: list[UsableChatModelView]


class ModelAccessPrincipalView(BaseModel):
    principal_type: str  # user | group | organization_unit | position
    principal_id: uuid.UUID
    # "allow" (default) or "deny" — a deny row excludes that principal
    # even if they also match an allow grant through a different
    # principal (e.g. their group). See services.model_access.
    effect: str = "allow"


class ModelAccessView(BaseModel):
    binding_id: uuid.UUID
    model_id: str
    # Empty principals = open to the whole org.
    principals: list[ModelAccessPrincipalView]


class SetModelAccessInput(BaseModel):
    binding_id: uuid.UUID
    model_id: str
    principals: list[ModelAccessPrincipalView] = Field(default_factory=list)


@router.get("/ai-usable-chat-models", response_model=UsableChatModelList)
async def list_usable_chat_models(
    for_me: bool = False,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> UsableChatModelList:
    # for_me=True → governance-filtered to what the caller may use (chat
    # picker). Default (admin/settings) → the full org catalog.
    if for_me:
        items = await ai_providers_service.list_usable_chat_models_for_user(
            session,
            organization_id=caller.organization.id,
            user_id=caller.user.id,
            identity_id=caller.user.identity_id,
        )
    else:
        items = await ai_providers_service.list_usable_chat_models(
            session, organization_id=caller.organization.id
        )
    return UsableChatModelList(
        items=[
            UsableChatModelView(
                model_id=m.model_id,
                display_name=m.display_name,
                binding_id=m.binding_id,
                binding_name=m.binding_name,
                is_platform_default=m.is_platform_default,
                vendor=m.vendor,
                is_default=m.is_default,
                is_private=m.is_private,
            )
            for m in items
        ]
    )


async def _require_org_admin(session: AsyncSession, caller: CallerContext) -> None:
    from quberi_api.services import authz

    ok = await authz.caller_has_org_role(
        session,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
        allowed_codes=("owner", "admin"),
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="model access governance is admin-only",
        )


@router.get("/ai-model-access", response_model=ModelAccessView)
async def get_model_access(
    binding_id: uuid.UUID,
    model_id: str,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ModelAccessView:
    from quberi_api.services import model_access as model_access_service

    await _require_org_admin(session, caller)
    principals = await model_access_service.list_policies(
        session,
        organization_id=caller.organization.id,
        binding_id=binding_id,
        model_id=model_id,
    )
    return ModelAccessView(
        binding_id=binding_id,
        model_id=model_id,
        principals=[
            ModelAccessPrincipalView(
                principal_type=p.principal_type,
                principal_id=p.principal_id,
                effect=p.effect,
            )
            for p in principals
        ],
    )


@router.put(
    "/ai-model-access",
    response_model=ModelAccessView,
    summary="Replace the access principals for a chat model (empty = open to all)",
)
async def set_model_access(
    payload: SetModelAccessInput,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ModelAccessView:
    from quberi_api.services import model_access as model_access_service

    await _require_org_admin(session, caller)
    try:
        await model_access_service.set_policies(
            session,
            organization_id=caller.organization.id,
            binding_id=payload.binding_id,
            model_id=payload.model_id,
            principals=[
                model_access_service.PolicyPrincipal(
                    principal_type=p.principal_type,
                    principal_id=p.principal_id,
                    effect=p.effect,
                )
                for p in payload.principals
            ],
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return ModelAccessView(
        binding_id=payload.binding_id,
        model_id=payload.model_id,
        principals=payload.principals,
    )


@router.get("/ai-models", response_model=list[ModelCapabilityView])
async def list_models(
    _: CallerContext = Depends(require_caller),
) -> list[ModelCapabilityView]:
    return [
        ModelCapabilityView(
            model_id=m.model_id,
            provider_kind=m.provider_kind,
            purpose=m.purpose,
            display_name=m.display_name,
            vector_dim=m.vector_dim,
            context_window=m.context_window,
            notes=m.notes,
        )
        for m in MODEL_REGISTRY.values()
    ]


@router.get("/ai-model-selections", response_model=ModelSelectionsView)
async def get_model_selections(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ModelSelectionsView:
    data = await ai_providers_service.get_selections_for_org(
        session, organization_id=caller.organization.id
    )
    return ModelSelectionsView(
        chat=data["chat"],
        embedding=data["embedding"],
        reranker=data["reranker"],
    )


@router.put("/ai-model-selections/{purpose}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def put_model_selection(
    purpose: str,
    payload: SetModelSelectionInput,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    if purpose not in ("chat", "embedding", "reranker"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid purpose")
    try:
        await ai_providers_service.set_selection(
            session,
            organization_id=caller.organization.id,
            purpose=purpose,
            model_id=payload.model_id,
            binding_id=payload.binding_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/ai-model-selections/{purpose}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_model_selection(
    purpose: str,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    if purpose not in ("chat", "embedding", "reranker"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid purpose")
    await ai_providers_service.clear_selection(
        session, organization_id=caller.organization.id, purpose=purpose
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


ai_providers_router = router
