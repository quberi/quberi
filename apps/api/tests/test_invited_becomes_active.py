"""Signing in is accepting the invitation: an invited person becomes active on first use."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from quberi_api.api import dependencies
from quberi_api.api.v1.spaces.router import router as spaces_router
from quberi_api.core.config import get_settings
from quberi_api.domain.identity.models import User
from quberi_api.services.auth.session import issue_session
from tests.test_image_search import _member


@pytest.fixture
async def client(db_session):
    app = FastAPI()
    app.state.settings = get_settings()
    app.include_router(spaces_router, prefix="/api/v1")

    async def _session():
        yield db_session

    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[dependencies.get_infrastructure] = lambda: SimpleNamespace()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _status(db, user_id):
    await db.commit()
    return (await db.execute(select(User.status).where(User.id == user_id))).scalar_one()


async def test_an_invited_person_who_signs_in_becomes_active(client, db_session, org_and_owner):
    db, boot = db_session, org_and_owner
    person = await _member(db, boot)
    uid, email = person.id, person.email
    await db.execute(update(User).where(User.id == uid).values(status="invited"))
    await db.commit()
    token = issue_session(
        identity_id=person.identity_id, user_id=uid, organization_id=boot.organization_id, email=email
    )

    resp = await client.get("/api/v1/spaces", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200, resp.text
    assert await _status(db, uid) == "active"


async def test_other_statuses_are_left_alone(client, db_session, org_and_owner):
    db, boot = db_session, org_and_owner
    person = await _member(db, boot)
    uid, email, identity = person.id, person.email, person.identity_id
    await db.execute(update(User).where(User.id == uid).values(status="suspended"))
    await db.commit()
    token = issue_session(
        identity_id=identity, user_id=uid, organization_id=boot.organization_id, email=email
    )
    await client.get("/api/v1/spaces", headers={"Authorization": f"Bearer {token}"})
    assert await _status(db, uid) == "suspended"
