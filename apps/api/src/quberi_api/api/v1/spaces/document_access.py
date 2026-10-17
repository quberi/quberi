"""Document-level access rules: share one document, or hide one document.

Endpoints
---------
* GET    /api/v1/spaces/{space_id}/documents/{document_id}/access-rules
* POST   /api/v1/spaces/{space_id}/documents/{document_id}/access-rules
* DELETE /api/v1/spaces/{space_id}/documents/{document_id}/access-rules/{rule_id}

``allow`` lets the subject read this one document without reading its space; ``deny``
hides it from the subject even if they can read the space, and wins over every ``allow``.
Only a space admin or owner (or an organisation owner/admin) may list or change rules.
A change applies to the next request: nothing is cached and nothing is re-indexed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.services.documents import access_rules as rules_service
from quberi_api.services.documents.errors import DocumentPermissionError

router = APIRouter(prefix="/spaces", tags=["document-access"])

SubjectType = Literal["user", "group", "organization_unit", "position", "organization"]
Effect = Literal["allow", "deny"]


class AddAccessRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effect: Effect
    subject_type: SubjectType
    # Omit for ``organization`` (everyone in the organisation): the server uses the
    # caller's own organisation, never one named by the client.
    subject_id: Optional[uuid.UUID] = None


class AccessRuleView(BaseModel):
    id: uuid.UUID
    effect: Effect
    subject_type: SubjectType
    subject_id: uuid.UUID
    subject_display_name: str
    subject_subtitle: Optional[str] = None
    created_at: datetime


class AccessRuleList(BaseModel):
    items: list[AccessRuleView] = Field(default_factory=list)
    total: int


class ManagedDocumentView(BaseModel):
    id: uuid.UUID
    name: str
    mime_type: str
    size_bytes: int
    updated_at: datetime
    # How many exceptions the document already has.
    allow_rules: int
    deny_rules: int


class ManagedDocumentList(BaseModel):
    items: list[ManagedDocumentView] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


def _to_response(view: rules_service.RuleView) -> AccessRuleView:
    rule = view.rule
    return AccessRuleView(
        id=rule.id,
        effect=rule.effect,  # type: ignore[arg-type]
        subject_type=rule.subject_type,  # type: ignore[arg-type]
        subject_id=rule.subject_id,
        subject_display_name=view.subject_display_name,
        subject_subtitle=view.subject_subtitle,
        created_at=rule.created_at,
    )


def _forbidden(exc: DocumentPermissionError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))


_PATH = "/{space_id}/documents/{document_id}/access-rules"


@router.get(
    "/{space_id}/access-rules/documents",
    response_model=ManagedDocumentList,
    summary="Names of a space's documents, for choosing one to set access on (managers only)",
)
async def list_managed_documents(
    space_id: uuid.UUID,
    q: Optional[str] = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ManagedDocumentList:
    """Metadata only: no text, no download. Lets an organisation admin find the document
    to share or hide in an enterprise space they cannot read themselves."""

    try:
        page = await rules_service.list_documents_for_manager(
            session,
            organization_id=caller.organization.id,
            actor_user_id=caller.user.id,
            space_id=space_id,
            q=q,
            limit=limit,
            offset=offset,
        )
    except DocumentPermissionError as exc:
        raise _forbidden(exc) from exc
    return ManagedDocumentList(
        items=[
            ManagedDocumentView(
                id=d.id,
                name=d.name,
                mime_type=d.mime_type,
                size_bytes=d.size_bytes,
                updated_at=d.updated_at,  # type: ignore[arg-type]
                allow_rules=d.allow_rules,
                deny_rules=d.deny_rules,
            )
            for d in page.items
        ],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )
@router.get(_PATH, response_model=AccessRuleList, summary="Who a document is shared with or hidden from")
async def list_access_rules(
    space_id: uuid.UUID,
    document_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AccessRuleList:
    try:
        views = await rules_service.list_rules(
            session,
            organization_id=caller.organization.id,
            actor_user_id=caller.user.id,
            space_id=space_id,
            document_id=document_id,
        )
    except DocumentPermissionError as exc:
        raise _forbidden(exc) from exc
    items = [_to_response(v) for v in views]
    return AccessRuleList(items=items, total=len(items))


@router.post(
    _PATH,
    status_code=status.HTTP_201_CREATED,
    response_model=AccessRuleView,
    summary="Share a document with someone, or hide it from someone",
)
async def add_access_rule(
    space_id: uuid.UUID,
    document_id: uuid.UUID,
    payload: AddAccessRuleRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AccessRuleView:
    subject_id = payload.subject_id
    if payload.subject_type == "organization" and subject_id is None:
        subject_id = caller.organization.id
    if subject_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="subject_id is required",
        )
    try:
        view = await rules_service.add_rule(
            session,
            organization_id=caller.organization.id,
            actor_user_id=caller.user.id,
            space_id=space_id,
            document_id=document_id,
            effect=payload.effect,
            subject_type=payload.subject_type,
            subject_id=subject_id,
        )
    except DocumentPermissionError as exc:
        raise _forbidden(exc) from exc
    return _to_response(view)


@router.delete(
    _PATH + "/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Remove one access rule from a document",
)
async def delete_access_rule(
    space_id: uuid.UUID,
    document_id: uuid.UUID,
    rule_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    try:
        await rules_service.remove_rule(
            session,
            organization_id=caller.organization.id,
            actor_user_id=caller.user.id,
            space_id=space_id,
            document_id=document_id,
            rule_id=rule_id,
        )
    except DocumentPermissionError as exc:
        raise _forbidden(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


document_access_router = router
