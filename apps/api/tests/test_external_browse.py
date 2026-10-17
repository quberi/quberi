# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""Browsing from outside tools: which spaces, and which documents in one.

``GET /api/v1/external/spaces`` and ``GET /api/v1/external/documents`` give a
tool a map of what the person has. They return metadata only: no document text
(search is the one path for that), no ids and no uploader. They run under the
read permission and the same live access rules as search, so a space the person
cannot see is not listed and a document they cannot see is not named.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from quberi_api.api.v1 import external as external_api
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document
from quberi_api.domain.identity.models import User
from quberi_api.domain.spaces.models import Space
from quberi_api.services import members as members_service
from quberi_api.services import space_bindings as space_bindings_service
from tests.test_external_save import (  # noqa: F401
    _auth,
    _no_infra,
    _org,
    _tokens,
    http,
    redis_client,
)
from tests.test_external_search_pending import _doc, _space

SPACES = "/api/v1/external/spaces"
DOCUMENTS = "/api/v1/external/documents"


async def _read(db, redis, boot, user_id=None):
    tokens = await _tokens(db, redis, boot, scope="knowledge:read", user_id=user_id)
    await db.commit()
    return _auth(tokens)


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


async def test_listing_needs_the_read_permission(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    tokens = await _tokens(
        db_session, redis_client, boot, scope="knowledge:write knowledge:upload"
    )
    await db_session.commit()
    for url in (SPACES, f"{DOCUMENTS}?space=x"):
        resp = await http.get(url, headers=_auth(tokens))
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "insufficient_scope"


async def test_listing_needs_a_token(http, _no_infra):
    assert (await http.get(SPACES)).status_code == 401
    assert (await http.get(f"{DOCUMENTS}?space=x")).status_code == 401


# ---------------------------------------------------------------------------
# Spaces
# ---------------------------------------------------------------------------


async def test_spaces_are_listed_with_kind_edit_right_and_counts(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    team = await _space(db_session, boot, "产品团队")
    await _doc(db_session, boot, team, "a.pdf", state="indexed")
    await _doc(db_session, boot, team, "b.pdf", state="indexing")
    await _doc(db_session, boot, team, "gone.pdf", state="indexed", status="archived")

    body = (await http.get(SPACES, headers=await _read(db_session, redis_client, boot))).json()

    by_name = {s["name"]: s for s in body["spaces"]}
    assert body["organization"].startswith("Org ")
    assert body["total"] == len(body["spaces"]) == 2
    personal = next(s for s in body["spaces"] if s["kind"] == "personal")
    assert personal["can_edit"] is True and personal["document_count"] == 0
    assert by_name["产品团队"] == {
        "name": "产品团队",
        "kind": "shared",
        "can_edit": True,
        "document_count": 2,  # the archived one is not counted
        "shared_only_documents": 0,
    }
    # Names and counts only: no ids, no owners, no document names.
    assert set(by_name["产品团队"]) == {
        "name", "kind", "can_edit", "document_count", "shared_only_documents"
    }
    assert "a.pdf" not in str(body)


async def test_a_read_only_space_says_the_person_cannot_edit_it(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    ro = await _space(db_session, boot, "只读区")
    invited = await members_service.invite_member(
        db_session,
        organization_id=boot.organization_id,
        invited_by_user_id=boot.user_id,
        email=f"m-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Member",
        role_code="member",
    )
    member = await db_session.get(User, invited.user.id)
    member.status = "active"
    await db_session.flush()
    await db_session.commit()
    await space_bindings_service.add_binding(
        db_session,
        space_id=ro.id,
        organization_id=boot.organization_id,
        actor_user_id=boot.user_id,
        subject_type="user",
        subject_id=member.id,
        role_code="viewer",
    )
    await db_session.commit()

    headers = await _read(db_session, redis_client, boot, user_id=member.id)
    body = (await http.get(SPACES, headers=headers)).json()

    by_name = {s["name"]: s for s in body["spaces"]}
    assert by_name["只读区"]["can_edit"] is False
    assert by_name["只读区"]["kind"] == "shared"


async def test_spaces_the_person_cannot_read_or_that_are_archived_are_not_listed(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    other = await _org(db_session)
    await _space(db_session, other, "别的公司")
    old = await _space(db_session, boot, "已归档")
    await db_session.execute(update(Space).where(Space.id == old.id).values(status="archived"))
    await db_session.commit()

    body = (await http.get(SPACES, headers=await _read(db_session, redis_client, boot))).json()

    names = {s["name"] for s in body["spaces"]}
    assert "别的公司" not in names
    assert "已归档" not in names


async def test_the_space_list_is_capped_but_the_total_is_honest(
    http, db_session, redis_client, _no_infra, monkeypatch
):
    boot = await _org(db_session)
    for i in range(4):
        await _space(db_session, boot, f"s{i}")
    monkeypatch.setattr(external_api, "_MAX_SPACES_LISTED", 3)

    body = (await http.get(SPACES, headers=await _read(db_session, redis_client, boot))).json()

    assert len(body["spaces"]) == 3
    assert body["total"] == 5  # four plus the personal space


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


async def test_documents_are_metadata_only(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    space = await _space(db_session, boot, "标准")
    await _doc(db_session, boot, space, "AI RMF.pdf", state="indexed")
    headers = await _read(db_session, redis_client, boot)

    resp = await http.get(DOCUMENTS, headers=headers, params={"space": "标准"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["space_name"] == "标准"
    assert body["total"] == 1
    assert body["next_cursor"] is None
    (doc,) = body["documents"]
    assert set(doc) == {"name", "mime_type", "size_bytes", "updated_at", "status"}
    assert doc["name"] == "AI RMF.pdf"
    assert doc["mime_type"] == "application/pdf"
    assert doc["status"] == "ready"
    for forbidden in ("uploaded_by", "uploader", "document_id", "text", "storage_key", "checksum"):
        assert forbidden not in str(body)


async def test_the_space_is_named_the_way_the_person_says_it(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    space = await _space(db_session, boot, "Product Team")
    await _doc(db_session, boot, space, "a.pdf", state="indexed")
    headers = await _read(db_session, redis_client, boot)

    resp = await http.get(DOCUMENTS, headers=headers, params={"space": "  product team "})

    assert resp.status_code == 200
    assert resp.json()["space_name"] == "Product Team"


async def test_status_tells_ready_from_indexing_from_not_searchable(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    space = await _space(db_session, boot, "标准")
    await _doc(db_session, boot, space, "ready.pdf", state="indexed")
    await _doc(db_session, boot, space, "working.pdf", state="indexing")
    await _doc(db_session, boot, space, "queued.pdf", state="not_indexed")
    await _doc(db_session, boot, space, "broken.pdf", state="failed")
    await _doc(db_session, boot, space, "stuck.pdf", state="indexing", age=timedelta(hours=3))
    headers = await _read(db_session, redis_client, boot)

    body = (await http.get(DOCUMENTS, headers=headers, params={"space": "标准"})).json()

    got = {d["name"]: d["status"] for d in body["documents"]}
    assert got == {
        "ready.pdf": "ready",
        "working.pdf": "indexing",
        "queued.pdf": "indexing",
        "broken.pdf": "not_searchable",
        "stuck.pdf": "not_searchable",
    }


async def test_archived_documents_are_left_out(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    space = await _space(db_session, boot, "标准")
    await _doc(db_session, boot, space, "kept.pdf", state="indexed")
    await _doc(db_session, boot, space, "gone.pdf", state="indexed", status="archived")
    headers = await _read(db_session, redis_client, boot)

    body = (await http.get(DOCUMENTS, headers=headers, params={"space": "标准"})).json()

    assert [d["name"] for d in body["documents"]] == ["kept.pdf"]
    assert body["total"] == 1


async def test_a_space_the_person_cannot_read_looks_like_one_that_does_not_exist(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    other = await _org(db_session)
    theirs = await _space(db_session, other, "机密")
    await _doc(db_session, other, theirs, "secret.pdf", state="indexed")
    headers = await _read(db_session, redis_client, boot)

    unreadable = await http.get(DOCUMENTS, headers=headers, params={"space": "机密"})
    await db_session.commit()  # the tests share one session; each request normally has its own
    missing = await http.get(DOCUMENTS, headers=headers, params={"space": "不存在"})

    assert unreadable.status_code == missing.status_code == 404
    codes = {unreadable.json()["detail"]["code"], missing.json()["detail"]["code"]}
    assert codes == {"space_not_found"}
    assert "secret.pdf" not in unreadable.text


async def test_a_row_from_another_organization_is_never_listed_even_in_this_space(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    other = await _org(db_session)
    space = await _space(db_session, boot, "标准")
    await _doc(db_session, boot, space, "mine.pdf", state="indexed")
    # Should never exist, but if a stray row did, the organisation filter still holds.
    await _doc(db_session, other, space, "stray.pdf", state="indexed")
    headers = await _read(db_session, redis_client, boot)

    body = (await http.get(DOCUMENTS, headers=headers, params={"space": "标准"})).json()

    assert [d["name"] for d in body["documents"]] == ["mine.pdf"]
    assert body["total"] == 1


async def test_an_archived_space_cannot_be_listed(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    space = await _space(db_session, boot, "已归档")
    await _doc(db_session, boot, space, "a.pdf", state="indexed")
    await db_session.execute(update(Space).where(Space.id == space.id).values(status="archived"))
    await db_session.commit()
    headers = await _read(db_session, redis_client, boot)

    resp = await http.get(DOCUMENTS, headers=headers, params={"space": "已归档"})

    assert resp.status_code == 404


async def test_the_space_is_required(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    headers = await _read(db_session, redis_client, boot)
    assert (await http.get(DOCUMENTS, headers=headers)).status_code == 422
    assert (await http.get(DOCUMENTS, headers=headers, params={"space": ""})).status_code == 422


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------


async def _many(db, boot, space, count, *, same_time: bool):
    stamp = datetime.now(timezone.utc) - timedelta(minutes=30)
    ids = []
    for i in range(count):
        doc = await _doc(db, boot, space, f"doc-{i:03d}.pdf", state="indexed")
        ids.append(doc.id)
    if same_time:
        # Many rows sharing one timestamp is the case that breaks a naive cursor.
        await db.execute(update(Document).where(Document.id.in_(ids)).values(updated_at=stamp))
    else:
        for i, doc_id in enumerate(ids):
            await db.execute(
                update(Document)
                .where(Document.id == doc_id)
                .values(updated_at=stamp + timedelta(seconds=i))
            )
    await db.commit()
    return ids


async def _walk(http, headers, space, limit=None):
    pages, cursor = [], None
    while True:
        params = {"space": space}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        body = (await http.get(DOCUMENTS, headers=headers, params=params)).json()
        pages.append(body)
        cursor = body["next_cursor"]
        if cursor is None:
            return pages
        assert len(pages) < 20


async def test_pages_cover_every_document_once_in_order(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    space = await _space(db_session, boot, "大库")
    await _many(db_session, boot, space, 120, same_time=False)
    headers = await _read(db_session, redis_client, boot)

    pages = await _walk(http, headers, "大库")

    assert [len(p["documents"]) for p in pages] == [50, 50, 20]
    assert all(p["total"] == 120 for p in pages)
    names = [d["name"] for p in pages for d in p["documents"]]
    assert len(names) == len(set(names)) == 120
    assert names[0] == "doc-119.pdf"  # most recently updated first
    assert names[-1] == "doc-000.pdf"
    assert pages[-1]["next_cursor"] is None


async def test_rows_that_share_a_timestamp_are_neither_skipped_nor_repeated(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    space = await _space(db_session, boot, "同时")
    await _many(db_session, boot, space, 23, same_time=True)
    headers = await _read(db_session, redis_client, boot)

    pages = await _walk(http, headers, "同时", limit=10)

    names = [d["name"] for p in pages for d in p["documents"]]
    assert [len(p["documents"]) for p in pages] == [10, 10, 3]
    assert len(names) == len(set(names)) == 23


async def test_a_page_never_exceeds_the_limit(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    space = await _space(db_session, boot, "大库")
    headers = await _read(db_session, redis_client, boot)
    assert (
        await http.get(DOCUMENTS, headers=headers, params={"space": "大库", "limit": 51})
    ).status_code == 422
    assert (
        await http.get(DOCUMENTS, headers=headers, params={"space": "大库", "limit": 0})
    ).status_code == 422
    assert space is not None


async def test_a_bad_cursor_is_refused(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    await _space(db_session, boot, "大库")
    headers = await _read(db_session, redis_client, boot)
    for bad in ("not-a-cursor", "e30", "eyJ0IjoieCIsImkiOiJ5In0", "!!!"):
        resp = await http.get(DOCUMENTS, headers=headers, params={"space": "大库", "cursor": bad})
        assert resp.status_code == 400, bad
        assert resp.json()["detail"]["code"] == "invalid_cursor"


async def test_a_cursor_only_marks_a_position_it_cannot_reach_another_space(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    first = await _space(db_session, boot, "甲")
    second = await _space(db_session, boot, "乙")
    await _many(db_session, boot, first, 3, same_time=False)
    await _doc(db_session, boot, second, "only-in-yi.pdf", state="indexed")
    headers = await _read(db_session, redis_client, boot)
    cursor = external_api._encode_cursor(datetime.now(timezone.utc), uuid.uuid4())

    body = (
        await http.get(DOCUMENTS, headers=headers, params={"space": "乙", "cursor": cursor})
    ).json()

    assert [d["name"] for d in body["documents"]] == ["only-in-yi.pdf"]
    assert not any(d["name"].startswith("doc-") for d in body["documents"])


# ---------------------------------------------------------------------------
# Rate limit and audit
# ---------------------------------------------------------------------------


async def test_both_listings_share_one_rate_limit(
    http, db_session, redis_client, _no_infra, monkeypatch
):
    boot = await _org(db_session)
    await _space(db_session, boot, "甲")
    headers = await _read(db_session, redis_client, boot)
    monkeypatch.setattr(external_api, "_LIST_RATE_LIMIT", 2)

    assert (await http.get(SPACES, headers=headers)).status_code == 200
    assert (await http.get(DOCUMENTS, headers=headers, params={"space": "甲"})).status_code == 200
    third = await http.get(SPACES, headers=headers)
    assert third.status_code == 429
    assert third.headers["retry-after"] == "60"


async def test_listings_are_audited_without_names(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    space = await _space(db_session, boot, "机密项目")
    await _doc(db_session, boot, space, "绝密文件.pdf", state="indexed")
    headers = await _read(db_session, redis_client, boot)

    await http.get(SPACES, headers=headers)
    await http.get(DOCUMENTS, headers=headers, params={"space": "机密项目"})

    await db_session.commit()
    events = (
        (
            await db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.organization_id == boot.organization_id,
                    AuditEvent.action == "knowledge.external_list",
                )
            )
        )
        .scalars()
        .all()
    )
    assert sorted(e.payload["what"] for e in events) == ["documents", "spaces"]
    assert all(e.actor_id == boot.user_id for e in events)
    docs_event = next(e for e in events if e.payload["what"] == "documents")
    assert docs_event.payload["space_id"] == str(space.id)
    assert docs_event.payload["result_count"] == 1
    assert "绝密文件" not in str([e.payload for e in events])
    assert "机密项目" not in str([e.payload for e in events])
