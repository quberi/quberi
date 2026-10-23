"""Message codes on the admin model-key and organisation routes.

Both routers are mounted on a small app with the admin check lifted (it has its own tests)
and the same exception handlers the real app registers.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from quberi_api.api import dependencies
from quberi_api.api.errors import register_exception_handlers
from quberi_api.api.security import require_platform_admin
from quberi_api.api.v1.admin.ai_providers import admin_ai_providers_router
from quberi_api.api.v1.admin.organizations import router as admin_organizations_router


@pytest.fixture
async def admin(db_session):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(admin_ai_providers_router, prefix="/api/v1")
    app.include_router(admin_organizations_router, prefix="/api/v1")

    async def _session():
        yield db_session

    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[require_platform_admin] = lambda: None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_an_unknown_platform_key_is_404_with_a_code(admin):
    resp = await admin.patch(
        f"/api/v1/admin/ai-providers/{uuid.uuid4()}", json={"status": "paused"}
    )
    assert resp.status_code == 404
    assert resp.json() == {
        "detail": "platform binding not found",
        "code": "admin.models.binding_not_found",
    }
    zh = await admin.patch(
        f"/api/v1/admin/ai-providers/{uuid.uuid4()}",
        json={"status": "paused"},
        headers={"Accept-Language": "zh-CN"},
    )
    assert zh.json()["code"] == "admin.models.binding_not_found"
    assert zh.json()["detail"] == "找不到该平台密钥"


async def test_an_invalid_visibility_mode_is_400_with_the_mode_as_a_param(admin):
    org_id = uuid.uuid4()
    resp = await admin.put(
        f"/api/v1/admin/organizations/{org_id}/platform-model-visibility",
        json={"mode": "everything"},
    )
    assert resp.status_code == 400
    assert resp.json() == {
        "detail": "invalid platform_model_visibility mode 'everything'",
        "code": "admin.models.visibility_mode_invalid",
        "params": {"mode": "'everything'"},
    }


async def test_visibility_for_an_unknown_organisation_is_400_with_the_id_as_a_param(admin):
    org_id = uuid.uuid4()
    resp = await admin.put(
        f"/api/v1/admin/organizations/{org_id}/platform-model-visibility",
        json={"mode": "all"},
    )
    assert resp.status_code == 400
    assert resp.json() == {
        "detail": f"organization {org_id} not found",
        "code": "admin.models.org_not_found",
        "params": {"id": str(org_id)},
    }
    zh = await admin.put(
        f"/api/v1/admin/organizations/{org_id}/platform-model-visibility",
        json={"mode": "all"},
        headers={"Accept-Language": "zh-CN"},
    )
    assert zh.json()["detail"] == f"找不到组织 {org_id}"


async def test_an_unknown_organisation_is_404_with_a_code(admin):
    org_id = uuid.uuid4()
    resp = await admin.get(f"/api/v1/admin/organizations/{org_id}")
    assert resp.status_code == 404
    assert resp.json() == {
        "detail": f"organization not found: {org_id}",
        "code": "admin.orgs.not_found",
        "params": {"id": str(org_id)},
    }
    zh = await admin.get(
        f"/api/v1/admin/organizations/{org_id}", headers={"Accept-Language": "zh-CN"}
    )
    assert zh.json()["detail"] == f"找不到组织：{org_id}"
