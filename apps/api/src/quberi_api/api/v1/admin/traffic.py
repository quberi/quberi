"""Platform-admin Traffic page: a summary of the reverse proxy's access log.

Endpoint
--------
GET  /api/v1/admin/traffic?hours=24&exclude_ips=203.0.113.9,2001:db8::1
    Who is visiting, split into real people and crawlers, scanners, MCP directories and MCP
    clients, from the proxy's JSON access log (``QUBERI_ACCESS_LOG_PATH``). ``hours`` is 1..336.
    ``exclude_ips`` is a comma list of full addresses whose requests are left out (the operator's
    own). With no log configured, or a missing/unreadable file, the answer is 200 with
    ``enabled: false`` and a ``reason``.

The kinds are a heuristic (see ``services.traffic_summary``). The answer never carries a raw log
line or a full address; User-Agents, hosts and paths are cut and returned as JSON text only.

Requires the same platform-admin credentials as every other ``/admin`` route.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from quberi_api.api.security import require_platform_admin
from quberi_api.services import traffic_summary
from quberi_api.services.admin_messages import pick_language, render

ADDRESS_NOTE_CODE = "admin.traffic.address_note"

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_platform_admin)],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TrafficWindowView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    to: str


class TrafficTotalsView(BaseModel):
    requests: int
    distinct_visitors: int
    automated_requests: int
    automated_share: float


class TrafficSiteView(BaseModel):
    host: str
    requests: int


class TrafficKindView(BaseModel):
    kind: str
    requests: int


class TrafficStatusClassView(BaseModel):
    status_class: str
    requests: int


class TrafficCountryView(BaseModel):
    country: str
    requests: int


class TrafficUserAgentView(BaseModel):
    user_agent: str
    kind: str
    requests: int


class TrafficPathView(BaseModel):
    method: str
    path: str
    status: int
    requests: int


class TrafficMcpView(BaseModel):
    top_user_agents: list[TrafficUserAgentView]
    top_paths: list[TrafficPathView]


class TrafficWebPageView(BaseModel):
    path: str
    browser_like: int
    search_engine: int
    total: int


class TrafficSignalsView(BaseModel):
    guest_questions: int
    sign_in_requests: int
    oauth_requests: int
    mcp_successful_posts: int


class TrafficVisitorView(BaseModel):
    address: str
    address_source: str
    kind: str
    country: str
    user_agent: str
    requests: int


class TrafficHourView(BaseModel):
    hour: str
    requests: int
    automated_requests: int


class TrafficSummaryView(BaseModel):
    enabled: bool
    reason: Optional[str] = None
    window: TrafficWindowView
    files_read: int
    truncated_by_cap: bool
    address_note: str
    # Stable code for ``address_note``; the console shows its own text for it.
    address_note_code: Optional[str] = None
    totals: TrafficTotalsView
    by_site: list[TrafficSiteView]
    by_kind: list[TrafficKindView]
    by_status_class: list[TrafficStatusClassView]
    by_country: list[TrafficCountryView]
    mcp: TrafficMcpView
    web_pages: list[TrafficWebPageView]
    signals: TrafficSignalsView
    top_visitors: list[TrafficVisitorView]
    hourly: list[TrafficHourView]


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get(
    "/traffic",
    response_model=TrafficSummaryView,
    summary="Summary of the proxy access log (people vs crawlers)",
)
async def get_traffic(
    request: Request,
    hours: int = Query(default=24, ge=1, le=336),
    exclude_ips: str = Query(default="", max_length=1000),
) -> dict:
    result = await traffic_summary.get_traffic(
        request.app.state.settings, hours=hours, exclude_ips_raw=exclude_ips
    )
    # The summary is cached for a few seconds and shared by every caller, so the note is
    # localised on a shallow copy and the cached entry is never changed.
    lang = pick_language(request.headers.get("accept-language"))
    return {
        **result,
        "address_note": render(ADDRESS_NOTE_CODE, lang),
        "address_note_code": ADDRESS_NOTE_CODE,
    }


admin_traffic_router = router
