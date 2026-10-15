"""The member-session note route must stay closed to guests and anonymous callers.

``POST /api/v1/spaces/{space_id}/notes`` is a normal write endpoint for signed-in
members. Guests (the read-only demo sessions) are refused by the app-wide guard
for every mutating method, not per route, so a newly added write route is
covered automatically. This test mounts the REAL guard and the REAL router, so
it cannot silently stop being true.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from quberi_api.api import dependencies
from quberi_api.api.v1.spaces.documents import space_documents_router
from quberi_api.core.config import get_settings
from quberi_api.main import _install_guest_readonly_guard
from quberi_api.services.auth.session import issue_session
from quberi_api.services.oauth import core as oauth_core

NOTE = {"title": "n", "text": "body"}


def _path() -> str:
    return f"/api/v1/spaces/{uuid.uuid4()}/notes"


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client(db_session):
    app = FastAPI()
    app.state.settings = get_settings()
    app.include_router(space_documents_router, prefix="/api/v1")
    _install_guest_readonly_guard(app)

    async def _session():
        yield db_session

    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[dependencies.get_infrastructure] = lambda: SimpleNamespace()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_a_guest_session_cannot_post_a_note(client):
    guest = issue_session(
        identity_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        email="guest@quberi.local",
        is_guest=True,
    )
    resp = await client.post(_path(), json=NOTE, headers=_bearer(guest))
    assert resp.status_code == 403
    assert resp.json().get("code") == "guest_read_only"


async def test_an_anonymous_caller_cannot_post_a_note(client):
    resp = await client.post(_path(), json=NOTE)
    assert resp.status_code == 401


async def test_an_external_tool_token_is_not_a_member_session_here(client):
    """An OAuth access token must not open the member note route."""
    resp = await client.post(
        _path(), json=NOTE, headers=_bearer(f"{oauth_core.ACCESS_PREFIX}not-a-member-session")
    )
    assert resp.status_code == 401
