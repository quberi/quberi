# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""Search says which documents are still being indexed.

A file uploaded a moment ago has no searchable text yet, so searching for it
returns whatever else is nearby with a middling score, which reads like "found
it" when it is not. The search response therefore lists the documents in the
spaces just searched that are still queued or being indexed, newest first, so a
tool can tell the person "that one is still processing" instead.

What these tests pin: only unfinished documents are listed (not finished,
failed or archived ones); only in spaces the person can read; only recent ones;
the list is capped while the total stays honest; and an ordinary search with
nothing pending is unchanged.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import update

from quberi_api.api.v1 import external as external_api
from quberi_api.domain.documents.models import Document
from quberi_api.services import document_sources as sources_service
from quberi_api.services import spaces as spaces_service
from tests.test_external_save import (  # noqa: F401
    _auth,
    _no_infra,
    _org,
    _tokens,
    http,
    redis_client,
)

SEARCH = "/api/v1/external/search"


async def _space(db, boot, name):
    await db.commit()
    view = await spaces_service.create_collaborative_space(
        db,
        organization_id=boot.organization_id,
        creator_user_id=boot.user_id,
        name=name,
        slug=f"s-{uuid.uuid4().hex[:8]}",
    )
    await db.commit()
    return view.space


async def _doc(db, boot, space, name, *, state="indexing", status="uploaded", age=timedelta(0)):
    local = await sources_service.ensure_local_source(db, organization_id=boot.organization_id)
    await db.commit()
    doc = Document(
        organization_id=boot.organization_id,
        space_id=space.id,
        source_id=local.id,
        name=name,
        mime_type="application/pdf",
        size_bytes=1,
        checksum_sha256="0" * 64,
        external_ref={},
        indexing_state=state,
        status=status,
    )
    db.add(doc)
    await db.flush()
    await db.commit()
    if age:
        # updated_at is maintained by the database layer; backdate it explicitly.
        await db.execute(
            update(Document)
            .where(Document.id == doc.id)
            .values(updated_at=datetime.now(timezone.utc) - age)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    return doc


def _stub_retrieval(monkeypatch, space_ids):
    async def fake_retrieve(session, *, qdrant, organization_id, caller_user_id, query, top_k):
        return SimpleNamespace(chunks=[], space_ids=list(space_ids))

    monkeypatch.setattr(external_api, "retrieve_for_user", fake_retrieve)


async def _search(http, db_session, redis_client, boot):
    tokens = await _tokens(db_session, redis_client, boot, scope="knowledge:read")
    await db_session.commit()
    resp = await http.post(SEARCH, headers=_auth(tokens), json={"query": "ai rmf"})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_unfinished_documents_are_listed_with_their_space(
    http, db_session, redis_client, _no_infra, monkeypatch
):
    boot = await _org(db_session)
    space = await _space(db_session, boot, "Standards")
    await _doc(db_session, boot, space, "AI RMF.pdf", state="indexing")
    await _doc(db_session, boot, space, "queued.docx", state="not_indexed")
    _stub_retrieval(monkeypatch, [space.id])

    body = await _search(http, db_session, redis_client, boot)

    assert body["pending_total"] == 2
    listed = {(d["document_name"], d["space_name"], d["state"]) for d in body["pending_documents"]}
    assert listed == {
        ("AI RMF.pdf", "Standards", "indexing"),
        ("queued.docx", "Standards", "not_indexed"),
    }


async def test_finished_failed_and_archived_documents_are_not_pending(
    http, db_session, redis_client, _no_infra, monkeypatch
):
    boot = await _org(db_session)
    space = await _space(db_session, boot, "Standards")
    await _doc(db_session, boot, space, "done.pdf", state="indexed")
    await _doc(db_session, boot, space, "broken.pdf", state="failed")
    await _doc(db_session, boot, space, "gone.pdf", state="indexing", status="archived")
    _stub_retrieval(monkeypatch, [space.id])

    body = await _search(http, db_session, redis_client, boot)

    assert body["pending_documents"] == []
    assert body["pending_total"] == 0


async def test_only_spaces_the_person_can_read_are_considered(
    http, db_session, redis_client, _no_infra, monkeypatch
):
    boot = await _org(db_session)
    mine = await _space(db_session, boot, "Mine")
    theirs = await _space(db_session, boot, "Not searched")
    await _doc(db_session, boot, mine, "visible.pdf")
    await _doc(db_session, boot, theirs, "hidden.pdf")
    _stub_retrieval(monkeypatch, [mine.id])

    body = await _search(http, db_session, redis_client, boot)

    assert [d["document_name"] for d in body["pending_documents"]] == ["visible.pdf"]
    assert body["pending_total"] == 1
    assert "hidden.pdf" not in str(body)


async def test_a_document_from_another_organization_is_never_listed(
    http, db_session, redis_client, _no_infra, monkeypatch
):
    boot = await _org(db_session)
    other = await _org(db_session)
    mine = await _space(db_session, boot, "Mine")
    foreign = await _space(db_session, other, "Foreign")
    await _doc(db_session, other, foreign, "foreign.pdf")
    # Even if retrieval handed back a foreign space id, the organisation filter holds.
    _stub_retrieval(monkeypatch, [mine.id, foreign.id])

    body = await _search(http, db_session, redis_client, boot)

    assert "foreign.pdf" not in str(body)


async def test_an_old_unfinished_row_is_not_reported_as_in_progress(
    http, db_session, redis_client, _no_infra, monkeypatch
):
    boot = await _org(db_session)
    space = await _space(db_session, boot, "Standards")
    await _doc(db_session, boot, space, "stale.pdf", state="indexing", age=timedelta(hours=3))
    await _doc(db_session, boot, space, "fresh.pdf", state="indexing", age=timedelta(minutes=5))
    _stub_retrieval(monkeypatch, [space.id])

    body = await _search(http, db_session, redis_client, boot)

    assert [d["document_name"] for d in body["pending_documents"]] == ["fresh.pdf"]
    assert body["pending_total"] == 1


async def test_the_list_is_capped_newest_first_and_the_total_stays_honest(
    http, db_session, redis_client, _no_infra, monkeypatch
):
    boot = await _org(db_session)
    space = await _space(db_session, boot, "Standards")
    for i in range(13):
        await _doc(db_session, boot, space, f"doc-{i:02d}.pdf", age=timedelta(minutes=13 - i))
    _stub_retrieval(monkeypatch, [space.id])

    body = await _search(http, db_session, redis_client, boot)

    assert body["pending_total"] == 13
    names = [d["document_name"] for d in body["pending_documents"]]
    assert len(names) == 10
    assert names[0] == "doc-12.pdf"  # most recently changed first
    assert "doc-00.pdf" not in names


async def test_nothing_pending_leaves_an_ordinary_response(
    http, db_session, redis_client, _no_infra, monkeypatch
):
    boot = await _org(db_session)
    space = await _space(db_session, boot, "Standards")
    _stub_retrieval(monkeypatch, [space.id])

    body = await _search(http, db_session, redis_client, boot)

    assert body["pending_documents"] == []
    assert body["pending_total"] == 0
    assert body["searched_spaces"] == 1
    assert body["passages"] == []
