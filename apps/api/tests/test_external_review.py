# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""What an outside tool is told about a file that is waiting for approval.

A file uploaded into a space that approves uploads is stored but not indexed. Left unsaid, it
would look like it is "still being indexed" for ever. So the upload and save answers say it is
waiting, the person's own document list shows it as awaiting approval, and a search names it
as awaiting approval rather than as being processed. Nobody else's waiting file shows up.
"""

from __future__ import annotations

import pytest

from quberi_api.services.documents import review
from tests.test_external_save import _auth, _no_infra, _org, _tokens, http, redis_client  # noqa: F401
from tests.test_image_indexing import _space
from tests.test_image_search import _grant, _member

UPLOAD = "/api/v1/external/upload"
SAVE = "/api/v1/external/save"
SPACES = "/api/v1/external/spaces"
DOCS = "/api/v1/external/documents"
SEARCH = "/api/v1/external/search"
PDF = b"%PDF-1.7\n" + b"x" * 64
ALL = "knowledge:read knowledge:write knowledge:upload"


async def _setup(db, redis, *, reviewed=True):
    boot = await _org(db)
    space = await _space(db, boot, "Wiki")
    space_id = space.id
    editor = await _member(db, boot)
    other = await _member(db, boot)
    editor_id, other_id = editor.id, other.id
    admin = await _member(db, boot)
    admin_id = admin.id
    for user, role in ((editor_id, "editor"), (other_id, "editor"), (admin_id, "admin")):
        await _grant(db, boot, space, type("U", (), {"id": user}), role=role)
    if reviewed:
        await db.commit()
        await review.set_requirement(
            db, organization_id=boot.organization_id, actor_user_id=boot.user_id,
            space_id=space_id, enabled=True,
        )
        await db.commit()
    tokens = {}
    for name, uid in (("editor", editor_id), ("other", other_id), ("admin", admin_id)):
        tokens[name] = await _tokens(db, redis, boot, scope=ALL, user_id=uid)
        await db.commit()
    return boot, tokens


async def _upload(http, tokens, name="plan.pdf"):
    return await http.post(
        UPLOAD, headers=_auth(tokens), files={"file": (name, PDF, "application/pdf")},
        data={"space": "Wiki"},
    )


async def test_an_upload_into_a_reviewed_space_says_it_is_waiting(
    http, db_session, redis_client, _no_infra
):
    _boot, tokens = await _setup(db_session, redis_client)
    waiting = await _upload(http, tokens["editor"])
    assert waiting.status_code == 201, waiting.text
    assert waiting.json()["review_state"] == "pending"
    assert "enqueued" not in _no_infra  # and it was not queued for indexing

    straight_in = await _upload(http, tokens["admin"], "admin.pdf")
    assert straight_in.json()["review_state"] == "approved"
    assert _no_infra["enqueued"] is not None


async def test_a_note_saved_into_a_reviewed_space_says_it_is_waiting(
    http, db_session, redis_client, _no_infra
):
    _boot, tokens = await _setup(db_session, redis_client)
    saved = await http.post(
        SAVE, headers=_auth(tokens["editor"]), json={"title": "idea", "text": "body", "space": "Wiki"}
    )
    assert saved.status_code == 201, saved.text
    assert saved.json()["review_state"] == "pending"


async def test_without_the_setting_nothing_changes(http, db_session, redis_client, _no_infra):
    _boot, tokens = await _setup(db_session, redis_client, reviewed=False)
    resp = await _upload(http, tokens["editor"])
    assert resp.json()["review_state"] == "approved"


async def test_the_uploader_sees_their_waiting_file_as_awaiting_approval_and_nobody_else_does(
    http, db_session, redis_client, _no_infra
):
    _boot, tokens = await _setup(db_session, redis_client)
    await _upload(http, tokens["editor"], "mine.pdf")
    await db_session.commit()

    mine = (await http.get(DOCS, headers=_auth(tokens["editor"]), params={"space": "Wiki"})).json()
    assert [(d["name"], d["status"]) for d in mine["documents"]] == [("mine.pdf", "awaiting_approval")]
    assert mine["total"] == 1
    await db_session.commit()
    theirs = (await http.get(DOCS, headers=_auth(tokens["other"]), params={"space": "Wiki"})).json()
    assert theirs["documents"] == [] and theirs["total"] == 0

    # The space count agrees with the list, for each of them.
    await db_session.commit()
    for who, expected in (("editor", 1), ("other", 0)):
        spaces = (await http.get(SPACES, headers=_auth(tokens[who]))).json()["spaces"]
        assert next(s for s in spaces if s["name"] == "Wiki")["document_count"] == expected
        await db_session.commit()


async def test_the_uploader_can_read_why_a_file_was_rejected_and_nobody_else_can(
    http, db_session, redis_client, _no_infra
):
    from sqlalchemy import select

    from quberi_api.domain.documents.models import Document

    boot, tokens = await _setup(db_session, redis_client)
    await _upload(http, tokens["editor"], "mine.pdf")
    await db_session.commit()
    doc_id = (await db_session.execute(select(Document.id).where(Document.name == "mine.pdf"))).scalar_one()
    await db_session.commit()
    await review.decide(
        db_session, organization_id=boot.organization_id, actor_user_id=boot.user_id,
        space_id=(await db_session.get(Document, doc_id)).space_id, document_ids=[doc_id],
        approve=False, note="contains customer data",
    )
    await db_session.commit()

    mine = (await http.get(DOCS, headers=_auth(tokens["editor"]), params={"space": "Wiki"})).json()
    assert [(d["status"], d["review_note"]) for d in mine["documents"]] == [
        ("rejected", "contains customer data")
    ]
    await db_session.commit()
    theirs = (await http.get(DOCS, headers=_auth(tokens["other"]), params={"space": "Wiki"})).json()
    assert theirs["documents"] == []
    await db_session.commit()
    # A file that is merely waiting has no reason to show.
    await _upload(http, tokens["editor"], "next.pdf")
    await db_session.commit()
    again = (await http.get(DOCS, headers=_auth(tokens["editor"]), params={"space": "Wiki"})).json()
    waiting = next(d for d in again["documents"] if d["name"] == "next.pdf")
    assert waiting["status"] == "awaiting_approval" and waiting["review_note"] is None


async def test_a_search_names_the_waiting_file_as_awaiting_approval_for_its_uploader(
    http, db_session, redis_client, _no_infra, monkeypatch
):
    from types import SimpleNamespace

    from quberi_api.services.doc_acl import NO_DOC_ACCESS
    from quberi_api.services.mcp import adapters

    boot, tokens = await _setup(db_session, redis_client)
    await _upload(http, tokens["editor"], "mine.pdf")
    await db_session.commit()
    space_ids = []

    async def fake_retrieve(session, *, qdrant, organization_id, caller_user_id, query, top_k, space_ids=None):
        from sqlalchemy import select

        from quberi_api.domain.spaces.models import Space

        ids = list((await session.execute(select(Space.id).where(Space.name == "Wiki"))).scalars())
        return SimpleNamespace(chunks=[], space_ids=ids, doc_access=NO_DOC_ACCESS)

    monkeypatch.setattr(adapters, "retrieve_for_user", fake_retrieve)
    _ = space_ids, boot

    mine = (await http.post(SEARCH, headers=_auth(tokens["editor"]), json={"query": "plan"})).json()
    assert [(d["document_name"], d["state"]) for d in mine["pending_documents"]] == [
        ("mine.pdf", "awaiting_approval")
    ]
    await db_session.commit()
    theirs = (await http.post(SEARCH, headers=_auth(tokens["other"]), json={"query": "plan"})).json()
    assert theirs["pending_documents"] == [] and theirs["pending_total"] == 0
