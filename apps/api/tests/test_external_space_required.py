# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""A tool that names no space, for a person with no personal space.

Everyone gets a personal space at onboarding, and a save or upload with no
space named goes there. If it is somehow missing, the API must not guess: it
used to fall back to the first space the person could reach, which could be a
shared one they never chose. Now it answers ``409 space_required`` with the
names of the spaces they can reach, so the tool can ask which one - and stores
nothing until the person picks.
"""

from __future__ import annotations

import uuid

from sqlalchemy import update

from quberi_api.domain.spaces.models import Space
from quberi_api.services import spaces as spaces_service
from tests.test_external_save import (  # noqa: F401
    _auth,
    _no_infra,
    _org,
    _tokens,
    http,
    redis_client,
)

SAVE = "/api/v1/external/save"
UPLOAD = "/api/v1/external/upload"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
ALL = "knowledge:read knowledge:write knowledge:upload"


async def _without_personal_space(db, boot, *, shared_name: str):
    """The owner keeps a shared space but loses the personal-space preset."""

    shared = await spaces_service.create_collaborative_space(
        db,
        organization_id=boot.organization_id,
        creator_user_id=boot.user_id,
        name=shared_name,
        slug=f"sh-{uuid.uuid4().hex[:8]}",
    )
    await db.execute(
        update(Space).where(Space.id == boot.personal_space_id).values(preset="collaborative")
    )
    await db.commit()
    return shared.space


async def test_save_without_a_space_asks_instead_of_guessing(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    shared = await _without_personal_space(db_session, boot, shared_name="产品团队")
    tokens = await _tokens(db_session, redis_client, boot, scope=ALL)
    await db_session.commit()

    resp = await http.post(SAVE, headers=_auth(tokens), json={"title": "x", "text": "y"})

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "space_required"
    assert "产品团队" in detail["spaces"]
    assert "blob" not in _no_infra and "enqueued" not in _no_infra
    assert str(shared.id) not in resp.text  # names only, no ids


async def test_upload_without_a_space_asks_instead_of_guessing(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    await _without_personal_space(db_session, boot, shared_name="产品团队")
    tokens = await _tokens(db_session, redis_client, boot, scope=ALL)
    await db_session.commit()

    resp = await http.post(
        UPLOAD, headers=_auth(tokens), files={"file": ("a.png", PNG, "image/png")}
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "space_required"
    assert "产品团队" in resp.json()["detail"]["spaces"]
    assert "blob" not in _no_infra and "enqueued" not in _no_infra


async def test_naming_a_space_then_works(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    shared = await _without_personal_space(db_session, boot, shared_name="产品团队")
    tokens = await _tokens(db_session, redis_client, boot, scope=ALL)
    await db_session.commit()

    saved = await http.post(
        SAVE, headers=_auth(tokens), json={"title": "x", "text": "y", "space": "产品团队"}
    )
    assert saved.status_code == 201, saved.text
    assert saved.json()["space_id"] == str(shared.id)

    uploaded = await http.post(
        UPLOAD,
        headers=_auth(tokens),
        data={"space": "产品团队"},
        files={"file": ("a.png", PNG, "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["space_id"] == str(shared.id)


async def test_a_person_with_a_personal_space_is_unaffected(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    tokens = await _tokens(db_session, redis_client, boot, scope=ALL)
    await db_session.commit()

    resp = await http.post(SAVE, headers=_auth(tokens), json={"title": "x", "text": "y"})

    assert resp.status_code == 201, resp.text
    assert resp.json()["space_id"] == str(boot.personal_space_id)


async def test_an_unknown_name_is_still_a_plain_404(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    await _without_personal_space(db_session, boot, shared_name="产品团队")
    tokens = await _tokens(db_session, redis_client, boot, scope=ALL)
    await db_session.commit()

    resp = await http.post(
        SAVE, headers=_auth(tokens), json={"title": "x", "text": "y", "space": "nope"}
    )

    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "space_not_found"
