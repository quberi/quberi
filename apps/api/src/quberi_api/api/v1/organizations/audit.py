"""Audit log endpoints (organisation owners and admins only).

* ``GET /organizations/current/recent-events`` feeds the admin console's
  "Live Org Pulse" card.
* ``GET /organizations/current/audit-events`` is the searchable, paged log
  behind the audit page, ``.../audit-events/actions`` its filter choices and
  ``.../audit-events.csv`` an export of the same filter.

The log records who asked what and which documents were put in front of the
model, so every read is restricted to the people who administer the
organisation.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.admin_gate import require_org_admin
from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.services import audit as audit_service

_ADMIN_ONLY = "only organisation owners or admins can read the audit log"
# One export is capped so a huge log cannot tie up a worker or the browser.
_CSV_MAX_ROWS = 10_000


class RetrievedDocumentView(BaseModel):
    id: uuid.UUID
    # ``None`` once the document has been deleted.
    name: Optional[str] = None


class RecentEventView(BaseModel):
    model_config = ConfigDict(from_attributes=False)

    id: uuid.UUID
    created_at: datetime
    actor_type: str
    actor_id: Optional[uuid.UUID] = None
    actor_display_name: str = ""
    action: str
    resource_type: str
    resource_id: Optional[uuid.UUID] = None
    resource_display_name: str = ""
    space_id: Optional[uuid.UUID] = None
    space_display_name: Optional[str] = None
    payload: dict = Field(default_factory=dict)
    retrieved_documents: list[RetrievedDocumentView] = Field(default_factory=list)


class RecentEventList(BaseModel):
    items: list[RecentEventView] = Field(default_factory=list)
    total: int


class AuditEventPage(BaseModel):
    items: list[RecentEventView] = Field(default_factory=list)
    next_cursor: Optional[str] = None


class AuditActionList(BaseModel):
    items: list[str] = Field(default_factory=list)


router = APIRouter(
    prefix="/organizations/current",
    tags=["organization-audit"],
)


def _to_view(v: audit_service.RecentEventView) -> RecentEventView:
    return RecentEventView(
        id=v.id,
        created_at=v.created_at,  # type: ignore[arg-type]
        actor_type=v.actor_type,
        actor_id=v.actor_id,
        actor_display_name=v.actor_display_name,
        action=v.action,
        resource_type=v.resource_type,
        resource_id=v.resource_id,
        resource_display_name=v.resource_display_name,
        space_id=v.space_id,
        space_display_name=v.space_display_name,
        payload=v.payload,
        retrieved_documents=[RetrievedDocumentView(**d) for d in v.retrieved_documents],
    )


@router.get(
    "/recent-events",
    response_model=RecentEventList,
    summary="Newest N audit events for the caller's organisation",
)
async def list_recent_events(
    limit: int = Query(default=20, ge=1, le=100),
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> RecentEventList:
    await require_org_admin(caller, session, detail=_ADMIN_ONLY)
    views = await audit_service.list_recent_events(
        session,
        organization_id=caller.organization.id,
        limit=limit,
    )
    items = [_to_view(v) for v in views]
    return RecentEventList(items=items, total=len(items))


@router.get(
    "/audit-events",
    response_model=AuditEventPage,
    summary="Searchable, paged audit log",
)
async def search_audit_events(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: Optional[str] = Query(default=None, max_length=512),
    action: Optional[str] = Query(default=None, max_length=128),
    actor_id: Optional[uuid.UUID] = None,
    space_id: Optional[uuid.UUID] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AuditEventPage:
    await require_org_admin(caller, session, detail=_ADMIN_ONLY)
    page = await audit_service.search_events(
        session,
        organization_id=caller.organization.id,
        limit=limit,
        cursor=cursor,
        action=action,
        actor_id=actor_id,
        space_id=space_id,
        since=since,
        until=until,
    )
    return AuditEventPage(
        items=[_to_view(v) for v in page.items], next_cursor=page.next_cursor
    )


@router.get(
    "/audit-events/actions",
    response_model=AuditActionList,
    summary="Action codes that appear in this organisation's audit log",
)
async def list_audit_actions(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> AuditActionList:
    await require_org_admin(caller, session, detail=_ADMIN_ONLY)
    return AuditActionList(
        items=await audit_service.list_actions(
            session, organization_id=caller.organization.id
        )
    )


def csv_safe(value: object) -> str:
    """Text for a CSV cell that a spreadsheet will not run as a formula.

    Names in the log are typed by users. A cell that starts with ``=``, ``+``,
    ``-``, ``@`` (or a tab or carriage return) would be executed by Excel and
    similar tools, so those get a leading apostrophe.
    """

    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


@router.get(
    "/audit-events.csv",
    summary="Export the filtered audit log as CSV",
)
async def export_audit_events(
    action: Optional[str] = Query(default=None, max_length=128),
    actor_id: Optional[uuid.UUID] = None,
    space_id: Optional[uuid.UUID] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await require_org_admin(caller, session, detail=_ADMIN_ONLY)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "time",
            "actor",
            "actor_id",
            "action",
            "resource_type",
            "resource",
            "resource_id",
            "space",
            "retrieved_documents",
            "details",
        ]
    )
    cursor: Optional[str] = None
    written = 0
    while written < _CSV_MAX_ROWS:
        page = await audit_service.search_events(
            session,
            organization_id=caller.organization.id,
            limit=100,
            cursor=cursor,
            action=action,
            actor_id=actor_id,
            space_id=space_id,
            since=since,
            until=until,
        )
        for v in page.items:
            if written >= _CSV_MAX_ROWS:
                break
            docs = "; ".join(
                f"{d['name'] or '(deleted)'} [{d['id']}]" for d in v.retrieved_documents
            )
            writer.writerow(
                [
                    csv_safe(v.created_at.isoformat()),  # type: ignore[attr-defined]
                    csv_safe(v.actor_display_name or v.actor_type),
                    csv_safe(v.actor_id),
                    csv_safe(v.action),
                    csv_safe(v.resource_type),
                    csv_safe(v.resource_display_name),
                    csv_safe(v.resource_id),
                    csv_safe(v.space_display_name),
                    csv_safe(docs),
                    csv_safe(json.dumps(v.payload, ensure_ascii=False, default=str)),
                ]
            )
            written += 1
        cursor = page.next_cursor
        if cursor is None:
            break
    return Response(
        content="\ufeff" + out.getvalue(),  # BOM so Excel reads UTF-8 correctly
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit-log.csv"'},
    )


recent_events_router = router
