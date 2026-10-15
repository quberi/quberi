"""Document source registration + test/sync endpoints (org-scoped)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.core.config import get_settings
from quberi_api.services import document_sources as sources_service
from quberi_api.services import feishu_sources
from quberi_api.services.authz import caller_has_org_role

router = APIRouter(
    prefix="/organizations/current/document-sources",
    tags=["document-sources"],
)

SourceKind = Literal["feishu", "oss_aliyun", "s3_aws"]
CachingPolicy = Literal["mirror", "reference_only"]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SourceView(BaseModel):
    model_config = ConfigDict(from_attributes=False)

    id: uuid.UUID
    organization_id: uuid.UUID
    kind: str
    name: str
    config: dict = Field(default_factory=dict)
    caching_policy: str
    status: str
    credentials_present: bool
    last_synced_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class SourceList(BaseModel):
    items: list[SourceView]
    total: int


class RegisterSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: SourceKind
    name: str = Field(min_length=1, max_length=200)
    config: dict = Field(default_factory=dict)
    credentials: Optional[dict] = None
    caching_policy: CachingPolicy = "mirror"


class ConnectorResultView(BaseModel):
    ok: bool
    message: str
    details: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Serialiser
# ---------------------------------------------------------------------------


def _to_view(v: sources_service.DocumentSourceView) -> SourceView:
    s = v.source
    return SourceView(
        id=s.id,
        organization_id=s.organization_id,
        kind=s.kind,
        name=s.name,
        config=s.config or {},
        caching_policy=s.caching_policy,
        status=s.status,
        credentials_present=v.credentials_present,
        last_synced_at=s.last_synced_at,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


# ---------------------------------------------------------------------------
# Admin gate
# ---------------------------------------------------------------------------


async def _require_org_admin(
    caller: CallerContext, session: AsyncSession
) -> None:
    ok = await caller_has_org_role(
        session,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
        allowed_codes=("owner", "admin"),
    )
    await session.commit()
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only organisation owners or admins can manage document sources",
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=SourceList, summary="List registered document sources")
async def list_sources(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> SourceList:
    views = await sources_service.list_sources(
        session, organization_id=caller.organization.id
    )
    items = [_to_view(v) for v in views]
    return SourceList(items=items, total=len(items))


@router.post(
    "",
    response_model=SourceView,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new external document source",
)
async def register_source(
    payload: RegisterSourceRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> SourceView:
    await _require_org_admin(caller, session)
    if payload.kind == "feishu":
        # Fail early on a bad space or folder, then prove the *person* (not
        # just the shared app) can open the folder in Feishu.
        try:
            _, folder_token = await feishu_sources.prepare_config(
                session,
                organization_id=caller.organization.id,
                actor_user_id=caller.user.id,
                config=payload.config,
            )
            await session.commit()
            await feishu_sources.verify_user_can_open_folder(
                session,
                organization_id=caller.organization.id,
                user_id=caller.user.id,
                folder_token=folder_token,
                base_url=get_settings().feishu_base_url,
            )
        except feishu_sources.FeishuAuthRequiredError as exc:
            # 428, not 409: 409 already means "name taken" or "folder belongs to
            # another organisation", and the UI must tell these apart.
            raise HTTPException(
                status_code=status.HTTP_428_PRECONDITION_REQUIRED, detail=str(exc)
            ) from exc
        except feishu_sources.FeishuFolderAccessError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
            ) from exc
    view = await sources_service.register_source(
        session,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
        kind=payload.kind,
        name=payload.name,
        config=payload.config,
        credentials=payload.credentials,
        caching_policy=payload.caching_policy,
    )
    return _to_view(view)


@router.get(
    "/{source_id}",
    response_model=SourceView,
    summary="Fetch one document source",
)
async def get_source(
    source_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> SourceView:
    view = await sources_service.get_source(
        session, source_id=source_id, organization_id=caller.organization.id
    )
    return _to_view(view)


@router.delete(
    "/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Remove a document source (external sources only)",
)
async def delete_source(
    source_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await _require_org_admin(caller, session)
    await sources_service.delete_source(
        session,
        source_id=source_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{source_id}:test",
    response_model=ConnectorResultView,
    summary="Probe the connector — mostly returns 'not implemented in Slice E'",
)
async def test_source(
    source_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ConnectorResultView:
    # Probing a connector makes outbound calls with the platform credentials.
    await _require_org_admin(caller, session)
    result = await sources_service.test_source(
        session, source_id=source_id, organization_id=caller.organization.id
    )
    return ConnectorResultView(
        ok=result.ok, message=result.message, details=result.details
    )


@router.post(
    "/{source_id}:sync",
    response_model=ConnectorResultView,
    summary="Trigger a sync — local is a no-op, external stubs return not-implemented",
)
async def sync_source(
    source_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ConnectorResultView:
    await _require_org_admin(caller, session)
    result = await sources_service.sync_source(
        session,
        source_id=source_id,
        organization_id=caller.organization.id,
        actor_user_id=caller.user.id,
    )
    return ConnectorResultView(
        ok=result.ok, message=result.message, details=result.details
    )


document_sources_router = router
