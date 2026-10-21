# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""The document list tells a space manager which documents carry exception rules, and nobody else."""

from __future__ import annotations

from tests.test_document_access_explain import world  # noqa: F401
from tests.test_document_acl_api import _as, qdrant, web  # noqa: F401
from tests.test_external_save import _org, redis_client  # noqa: F401


async def _list(web, w):
    resp = await web.get(f"/api/v1/spaces/{w.space.id}/documents", params={"limit": 50})
    assert resp.status_code == 200, resp.text
    return {d["name"]: d for d in resp.json()["items"]}


async def test_a_space_manager_sees_how_many_rules_each_document_carries(web, db_session, world):
    w = world
    await _as(web, db_session, w.boot, w.space_owner.id)

    docs = await _list(web, w)

    assert (docs["plain.pdf"]["allow_rules"], docs["plain.pdf"]["deny_rules"]) == (1, 0)
    assert (docs["hidden.pdf"]["allow_rules"], docs["hidden.pdf"]["deny_rules"]) == (0, 1)
    # A document with no rules is reported as zero, not left out.
    assert docs["waiting.pdf"]["allow_rules"] == 0 and docs["waiting.pdf"]["deny_rules"] == 0


async def test_someone_who_only_reads_the_space_is_not_told(web, db_session, world):
    w = world
    await _as(web, db_session, w.boot, w.editor.id)

    docs = await _list(web, w)

    assert docs, "the editor can read the space"
    assert all(d["allow_rules"] is None and d["deny_rules"] is None for d in docs.values())
