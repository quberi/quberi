"""Platform-admin management of platform-shared AI provider bindings.

Lets a platform admin, from the ops console, do what used to require
editing ``.env`` and restarting the API: add/rotate/pause a platform
chat key for any vendor (DeepSeek, OpenAI, ...), set its priority among
other keys for the same vendor, and control which organisations may see
platform models at all (or only a curated subset).

Endpoints
---------
GET    /admin/ai-providers                           — list every platform binding
POST   /admin/ai-providers                            — register a new platform key
PATCH  /admin/ai-providers/{binding_id}                — edit/rotate/pause a platform key
DELETE /admin/ai-providers/{binding_id}                — remove a platform key
GET    /admin/ai-models                                — model registry catalog (for pickers)
GET    /admin/organizations/{organization_id}/platform-model-visibility
PUT    /admin/organizations/{organization_id}/platform-model-visibility

All require ``Authorization: Bearer <QUBERI_PLATFORM_ADMIN_TOKEN>``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.admin_errors import AdminApiException, from_coded
from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import require_platform_admin
from quberi_api.services import ai_providers as ai_providers_service
from quberi_api.services.admin_messages import CodedValueError
from quberi_api.services.ai.registry import MODEL_REGISTRY, vendor_display_name

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_platform_admin)],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PlatformBindingView(BaseModel):
    id: uuid.UUID
    provider_kind: str
    vendor: Optional[str] = None
    vendor_display_name: str = ""
    display_name: str
    base_url: Optional[str] = None
    api_key_present: bool
    status: str
    priority: int
    models: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class PlatformBindingList(BaseModel):
    items: list[PlatformBindingView]


class CreatePlatformBindingInput(BaseModel):
    provider_kind: str = Field(..., description="e.g. openai_compat")
    vendor: Optional[str] = Field(None, description="e.g. deepseek, openai, dashscope")
    display_name: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    priority: int = 100
    models: Optional[list[str]] = None


class UpdatePlatformBindingInput(BaseModel):
    display_name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[int] = None
    models: Optional[list[str]] = None


class ModelCatalogEntry(BaseModel):
    model_id: str
    provider_kind: str
    purpose: str
    display_name: str
    vendor: Optional[str] = None
    vendor_display_name: str = ""
    context_window: int
    notes: str
    # Whether the model reads pictures (it can then be offered as a vision model).
    supports_vision: bool = False


class PlatformModelVisibilityView(BaseModel):
    organization_id: uuid.UUID
    mode: str  # all | none | allowlist
    model_ids: list[str] = Field(default_factory=list)


class SetPlatformModelVisibilityInput(BaseModel):
    mode: str
    model_ids: Optional[list[str]] = None


def _view(v: ai_providers_service.ProviderBindingView) -> PlatformBindingView:
    return PlatformBindingView(
        id=v.id,
        provider_kind=v.provider_kind,
        vendor=v.vendor,
        vendor_display_name=vendor_display_name(v.vendor),
        display_name=v.display_name,
        base_url=v.base_url,
        api_key_present=v.api_key_present,
        status=v.status,
        priority=v.priority,
        models=v.models,
        created_at=v.created_at,
        updated_at=v.updated_at,
    )


# ---------------------------------------------------------------------------
# Platform binding CRUD
# ---------------------------------------------------------------------------


@router.get("/ai-providers", response_model=PlatformBindingList)
async def list_platform_bindings(
    session: AsyncSession = Depends(get_db_session),
) -> PlatformBindingList:
    views = await ai_providers_service.list_platform_bindings_full(session)
    return PlatformBindingList(items=[_view(v) for v in views])


@router.post(
    "/ai-providers",
    response_model=PlatformBindingView,
    status_code=status.HTTP_201_CREATED,
)
async def create_platform_binding(
    payload: CreatePlatformBindingInput,
    session: AsyncSession = Depends(get_db_session),
) -> PlatformBindingView:
    view = await ai_providers_service.register_platform_binding(
        session,
        provider_kind=payload.provider_kind,
        vendor=payload.vendor,
        display_name=payload.display_name,
        base_url=payload.base_url,
        api_key=payload.api_key,
        priority=payload.priority,
        models=payload.models,
    )
    return _view(view)


@router.patch("/ai-providers/{binding_id}", response_model=PlatformBindingView)
async def update_platform_binding(
    binding_id: uuid.UUID,
    payload: UpdatePlatformBindingInput,
    session: AsyncSession = Depends(get_db_session),
) -> PlatformBindingView:
    fields = payload.model_dump(exclude_unset=True)
    kwargs = {
        k: fields[k]
        for k in ("display_name", "base_url", "api_key", "status", "priority", "models")
        if k in fields
    }
    view = await ai_providers_service.update_platform_binding(
        session, binding_id=binding_id, **kwargs
    )
    if view is None:
        raise AdminApiException(
            status.HTTP_404_NOT_FOUND, "admin.models.binding_not_found"
        )
    return _view(view)


@router.delete(
    "/ai-providers/{binding_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_platform_binding(
    binding_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await ai_providers_service.delete_platform_binding(session, binding_id=binding_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/ai-models", response_model=list[ModelCatalogEntry])
async def list_model_catalog() -> list[ModelCatalogEntry]:
    return [
        ModelCatalogEntry(
            model_id=m.model_id,
            provider_kind=m.provider_kind,
            purpose=m.purpose,
            display_name=m.display_name,
            vendor=m.vendor,
            vendor_display_name=vendor_display_name(m.vendor),
            context_window=m.context_window,
            notes=m.notes,
            supports_vision=m.supports_vision,
        )
        for m in MODEL_REGISTRY.values()
    ]


# ---------------------------------------------------------------------------
# Per-org platform model visibility
# ---------------------------------------------------------------------------


@router.get(
    "/organizations/{organization_id}/platform-model-visibility",
    response_model=PlatformModelVisibilityView,
)
async def get_platform_model_visibility(
    organization_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
) -> PlatformModelVisibilityView:
    mode, model_ids = await ai_providers_service.get_org_platform_model_visibility(
        session, organization_id=organization_id
    )
    return PlatformModelVisibilityView(
        organization_id=organization_id, mode=mode, model_ids=model_ids
    )


@router.put(
    "/organizations/{organization_id}/platform-model-visibility",
    response_model=PlatformModelVisibilityView,
)
async def set_platform_model_visibility(
    organization_id: uuid.UUID,
    payload: SetPlatformModelVisibilityInput,
    session: AsyncSession = Depends(get_db_session),
) -> PlatformModelVisibilityView:
    try:
        await ai_providers_service.set_org_platform_model_visibility(
            session,
            organization_id=organization_id,
            mode=payload.mode,
            model_ids=payload.model_ids,
        )
    except CodedValueError as exc:
        raise from_coded(exc, status.HTTP_400_BAD_REQUEST) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    mode, model_ids = await ai_providers_service.get_org_platform_model_visibility(
        session, organization_id=organization_id
    )
    return PlatformModelVisibilityView(
        organization_id=organization_id, mode=mode, model_ids=model_ids
    )


admin_ai_providers_router = router
