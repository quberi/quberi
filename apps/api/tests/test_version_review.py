# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""A new version added where uploads need approval waits for a reviewer.

An editor's new version of a document, in a space that approves uploads, is stored but is not
the document's file: it is not indexed, not searchable and seen only by its uploader and the
space's reviewers. A reviewer approves it (it becomes the current file and is indexed) or
rejects it with a reason. Real: database, services, permission evaluator, web routes. Stood
in: the file store and the queue.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document, DocumentVersion
from quberi_api.services.documents import new_version, review, version_review
from quberi_api.services.documents import versions as versions_service
from quberi_api.services.documents.errors import DuplicateDocumentError, VersionConflictError
from quberi_api.services.errors import ForbiddenError, InvalidInputError, NotFoundError
from tests.test_document_versions import (  # noqa: F401
    V1,
    V2,
    V3,
    _new_version,
    _row,
    _upload,
    _versions,
    infra,
    world,
)
from tests.test_document_versions_flow import _as, _file, blobs, web  # noqa: F401

V4 = b"# Leave policy\n\nAnnual leave is 20 days.\n"


async def _require(db, w, on=True):
    await db.commit()
    await review.set_requirement(
        db,
        organization_id=w.boot.organization_id,
        actor_user_id=w.owner.id,
        space_id=w.space.id,
        enabled=on,
    )
    await db.commit()


async def _setup(db, w, uploader=None):
    """An approved document in a space that approves uploads."""
    doc = await _upload(db, w, w.admin)  # an admin's own upload is approved straight away
    await _require(db, w)
    return doc


async def _submit(db, w, who, doc, data=V2, **kw):
    """An editor's new version; returns the version row."""
    await _new_version(db, w, who, doc, data, **kw)
    return (await _versions(db, doc.id))[-1]


async def _decide(db, w, doc, number, *, approve=True, note="", who=None):
    await db.commit()
    return await version_review.decide(
        db,
        organization_id=w.boot.organization_id,
        actor_user_id=(who or w.admin).id,
        space_id=w.space.id,
        targets=[(doc.id, number)],
        approve=approve,
        note=note,
    )


# --------------------------------------------------------------------------
# Submitting
# --------------------------------------------------------------------------


async def test_an_editors_version_waits_and_the_document_keeps_its_file(db_session, world, infra):
    doc = await _setup(db_session, world)
    before = await _row(db_session, doc.id)
    keep = (before.storage_key, before.checksum_sha256, before.current_version_id, before.name)
    infra.jobs.clear()

    v2 = await _submit(db_session, world, world.editor, doc, note="10 days")

    assert (v2.version_number, v2.review_state, v2.created_by_user_id) == (
        2,
        "pending",
        world.editor.id,
    )
    after = await _row(db_session, doc.id)
    assert (after.storage_key, after.checksum_sha256, after.current_version_id, after.name) == keep
    assert infra.jobs == []  # nothing is indexed
    assert infra.removed == []
    # The file is stored under its own key and audited as submitted, not added.
    assert v2.storage_key != doc.storage_key
    actions = (
        (
            await db_session.execute(
                select(AuditEvent.action).where(AuditEvent.resource_id == doc.id)
            )
        )
        .scalars()
        .all()
    )
    assert "document.version_submitted" in actions and "document.version_added" not in actions


async def test_a_reviewers_version_is_current_at_once_as_before(db_session, world, infra):
    doc = await _setup(db_session, world)
    infra.jobs.clear()

    v2 = await _submit(db_session, world, world.admin, doc)

    assert v2.review_state == "approved"
    assert (await _row(db_session, doc.id)).current_version_id == v2.id
    assert infra.jobs == [doc.id]


async def test_where_uploads_need_no_approval_an_editors_version_is_current_at_once(
    db_session, world, infra
):
    doc = await _upload(db_session, world, world.editor)
    v2 = await _submit(db_session, world, world.editor, doc)
    assert v2.review_state == "approved"
    assert (await _row(db_session, doc.id)).current_version_id == v2.id


async def test_only_one_version_waits_at_a_time(db_session, world, infra):
    doc = await _setup(db_session, world)
    await _submit(db_session, world, world.editor, doc, V2)
    stored_before = len(infra.stored)

    for who in (world.editor, world.admin):
        with pytest.raises(VersionConflictError, match="waiting for approval"):
            await _new_version(db_session, world, who, doc, V3)

    assert len(await _versions(db_session, doc.id)) == 2
    assert infra.stored[stored_before:] == []  # refused before anything was stored


async def test_two_submissions_at_the_same_moment_cannot_both_wait(db_session, world, infra):
    doc = await _setup(db_session, world)

    async def someone_else_submits_first():
        db_session.add(
            DocumentVersion(
                id=uuid.uuid4(),
                document_id=doc.id,
                version_number=2,
                filename="x.md",
                mime_type="text/markdown",
                size_bytes=1,
                storage_key=f"k/{uuid.uuid4()}",
                checksum_sha256="0" * 64,
                review_state="pending",
            )
        )
        await db_session.commit()

    infra.on_store = someone_else_submits_first
    with pytest.raises(VersionConflictError):
        await _new_version(db_session, world, world.editor, doc, V3)
    assert [v.review_state for v in await _versions(db_session, doc.id)] == [
        "approved",
        "pending",
    ]
    assert infra.removed  # the file of the losing attempt is removed


async def test_the_same_bytes_as_the_current_file_are_refused_even_when_they_would_wait(
    db_session, world, infra
):
    doc = await _setup(db_session, world)
    with pytest.raises(VersionConflictError):
        await _new_version(db_session, world, world.editor, doc, V1)


async def test_a_file_already_in_the_space_is_refused_even_when_it_would_wait(
    db_session, world, infra
):
    doc = await _setup(db_session, world)
    await _upload(db_session, world, world.admin, "other.md", V2)
    with pytest.raises(DuplicateDocumentError):
        await _new_version(db_session, world, world.editor, doc, V2)


# --------------------------------------------------------------------------
# Who can see a version that waits
# --------------------------------------------------------------------------


async def _versions_seen_by(db, w, doc, who):
    rows = await new_version.list_document_versions(
        db,
        document_id=doc.id,
        organization_id=w.boot.organization_id,
        space_id=w.space.id,
        caller_user_id=who.id,
    )
    return [(r.version.version_number, r.version.review_state) for r in rows]


async def test_a_waiting_version_is_seen_by_its_uploader_and_the_reviewers_only(
    db_session, world, infra
):
    doc = await _setup(db_session, world)
    await _submit(db_session, world, world.editor, doc)
    other_editor = SimpleNamespace(id=world.boot.user_id)  # the owner reviews; use a viewer too

    waiting = [(2, "pending"), (1, "approved")]
    assert await _versions_seen_by(db_session, world, doc, world.editor) == waiting
    assert await _versions_seen_by(db_session, world, doc, world.admin) == waiting
    assert await _versions_seen_by(db_session, world, doc, other_editor) == waiting  # owner
    assert await _versions_seen_by(db_session, world, doc, world.viewer) == [(1, "approved")]


async def test_nobody_else_can_open_a_waiting_version(db_session, world, infra):
    doc = await _setup(db_session, world)
    await _submit(db_session, world, world.editor, doc)

    async def open_as(who):
        return await new_version.open_version_stream(
            db_session,
            document_id=doc.id,
            version_number=2,
            organization_id=world.boot.organization_id,
            space_id=world.space.id,
            caller_user_id=who.id,
        )

    for allowed in (world.editor, world.admin):
        version, _ = await open_as(allowed)
        assert version.review_state == "pending"
    with pytest.raises(NotFoundError):
        await open_as(world.viewer)


async def test_the_text_of_a_waiting_version_is_not_given_to_a_reader(db_session, world, infra):
    from quberi_api.services import documents as documents_service

    doc = await _setup(db_session, world)
    await _submit(db_session, world, world.editor, doc)
    await db_session.commit()

    async def text_as(who):
        return await documents_service.get_document_text(
            db_session,
            document_id=doc.id,
            organization_id=world.boot.organization_id,
            space_id=world.space.id,
            caller_user_id=who.id,
            version_number=2,
        )

    with pytest.raises(NotFoundError):
        await text_as(world.viewer)


async def test_the_summaries_a_list_needs_leave_out_what_waits(db_session, world, infra):
    doc = await _setup(db_session, world)
    await _submit(db_session, world, world.editor, doc)
    await db_session.commit()

    assert await versions_service.version_count_by_document(db_session, [doc.id]) == {doc.id: 1}
    assert await versions_service.latest_version_numbers(db_session, [doc.id]) == {doc.id: 1}
    ask = versions_service.waiting_version_numbers
    assert await ask(db_session, [doc.id], user_id=world.editor.id, reviewer=False) == {doc.id: 2}
    assert await ask(db_session, [doc.id], user_id=world.viewer.id, reviewer=False) == {}
    assert await ask(db_session, [doc.id], user_id=world.viewer.id, reviewer=True) == {doc.id: 2}


async def test_a_waiting_version_is_never_in_the_index_or_the_retrieval_label(
    db_session, world, infra
):
    from quberi_api.services.rag.retrieve import _label_latest_versions
    from tests.test_version_labels import _hit

    doc = await _setup(db_session, world)
    await _submit(db_session, world, world.editor, doc)
    await db_session.commit()
    labelled = await _label_latest_versions(db_session, [_hit(doc.id, 1)])
    assert labelled[0].latest_version == 1  # the waiting version 2 is not "the newest"


# --------------------------------------------------------------------------
# Deciding
# --------------------------------------------------------------------------


async def test_approving_makes_the_version_current_and_queues_indexing(db_session, world, infra):
    doc = await _setup(db_session, world)
    v2 = await _submit(db_session, world, world.editor, doc, V2, name="leave-2026.md")
    await db_session.execute(
        update(Document)
        .where(Document.id == doc.id)
        .values(indexing_state="indexed", chunk_count=3)
    )
    await db_session.commit()
    infra.jobs.clear()

    changed = await _decide(db_session, world, doc, 2, note="looks right")

    assert changed == [(doc.id, 2)]
    row = await _row(db_session, doc.id)
    assert row.current_version_id == v2.id and row.name == "leave-2026.md"
    assert (row.storage_key, row.checksum_sha256) == (v2.storage_key, v2.checksum_sha256)
    assert (row.indexing_state, row.chunk_count) == ("not_indexed", 0)
    assert infra.jobs == [doc.id]
    v1, v2 = await _versions(db_session, doc.id)
    assert (v2.review_state, v2.reviewed_by_user_id, v2.review_note) == (
        "approved",
        world.admin.id,
        "looks right",
    )
    assert v2.reviewed_at is not None
    assert v1.review_state == "approved"  # the history is untouched
    assert await versions_service.version_count_by_document(db_session, [doc.id]) == {doc.id: 2}
    actions = (
        (
            await db_session.execute(
                select(AuditEvent.action).where(AuditEvent.resource_id == doc.id)
            )
        )
        .scalars()
        .all()
    )
    assert "document.version_approved" in actions


async def test_after_approval_everyone_sees_it_and_a_new_one_can_be_submitted(
    db_session, world, infra
):
    doc = await _setup(db_session, world)
    await _submit(db_session, world, world.editor, doc)
    await _decide(db_session, world, doc, 2)

    assert await _versions_seen_by(db_session, world, doc, world.viewer) == [
        (2, "approved"),
        (1, "approved"),
    ]
    v3 = await _submit(db_session, world, world.editor, doc, V3)
    assert (v3.version_number, v3.review_state) == (3, "pending")


async def test_rejecting_needs_a_reason_and_leaves_the_document_alone(db_session, world, infra):
    doc = await _setup(db_session, world)
    await _submit(db_session, world, world.editor, doc)
    before = await _row(db_session, doc.id)
    infra.jobs.clear()

    with pytest.raises(InvalidInputError, match="say why"):
        await _decide(db_session, world, doc, 2, approve=False)
    await _decide(db_session, world, doc, 2, approve=False, note="wrong figures")

    after = await _row(db_session, doc.id)
    assert (after.current_version_id, after.storage_key) == (
        before.current_version_id,
        before.storage_key,
    )
    assert infra.jobs == []
    _, v2 = await _versions(db_session, doc.id)
    assert (v2.review_state, v2.review_note) == ("rejected", "wrong figures")
    # The uploader sees why; a reader does not see it at all.
    assert await _versions_seen_by(db_session, world, doc, world.editor) == [
        (2, "rejected"),
        (1, "approved"),
    ]
    assert await _versions_seen_by(db_session, world, doc, world.viewer) == [(1, "approved")]


async def test_after_a_rejection_the_next_version_takes_the_next_number(db_session, world, infra):
    doc = await _setup(db_session, world)
    await _submit(db_session, world, world.editor, doc, V2)
    await _decide(db_session, world, doc, 2, approve=False, note="no")

    v3 = await _submit(db_session, world, world.editor, doc, V3)

    assert (v3.version_number, v3.review_state) == (3, "pending")


async def test_a_refused_version_may_be_approved_later_if_nothing_newer_is_current(
    db_session, world, infra
):
    doc = await _setup(db_session, world)
    await _submit(db_session, world, world.editor, doc, V2)
    await _decide(db_session, world, doc, 2, approve=False, note="not yet")

    await _decide(db_session, world, doc, 2)

    assert (await _row(db_session, doc.id)).checksum_sha256 is not None
    assert [v.review_state for v in await _versions(db_session, doc.id)] == [
        "approved",
        "approved",
    ]


async def test_a_refused_version_cannot_be_approved_over_a_newer_current_one(
    db_session, world, infra
):
    doc = await _setup(db_session, world)
    await _submit(db_session, world, world.editor, doc, V2)
    await _decide(db_session, world, doc, 2, approve=False, note="not yet")
    await _submit(db_session, world, world.admin, doc, V3)  # version 3 is current

    with pytest.raises(InvalidInputError, match="newer version"):
        await _decide(db_session, world, doc, 2)

    assert [v.review_state for v in await _versions(db_session, doc.id)] == [
        "approved",
        "rejected",
        "approved",
    ]


async def test_a_version_that_was_decided_cannot_be_decided_again(db_session, world, infra):
    doc = await _setup(db_session, world)
    await _submit(db_session, world, world.editor, doc)
    await _decide(db_session, world, doc, 2)
    with pytest.raises(InvalidInputError, match="already approved"):
        await _decide(db_session, world, doc, 2)
    with pytest.raises(InvalidInputError, match="already approved"):
        await _decide(db_session, world, doc, 2, approve=False, note="no")


async def test_only_a_reviewer_decides(db_session, world, infra):
    doc = await _setup(db_session, world)
    await _submit(db_session, world, world.editor, doc)

    for who in (world.editor, world.viewer):
        with pytest.raises(ForbiddenError):
            await _decide(db_session, world, doc, 2, who=who)
    with pytest.raises(NotFoundError):
        await _decide(db_session, world, doc, 2, who=world.stranger)
    assert [v.review_state for v in await _versions(db_session, doc.id)] == [
        "approved",
        "pending",
    ]


async def test_a_version_cannot_be_decided_through_another_space(db_session, world, infra):
    doc = await _setup(db_session, world)
    await _submit(db_session, world, world.editor, doc)
    await db_session.commit()
    with pytest.raises(NotFoundError):
        await version_review.decide(
            db_session,
            organization_id=world.boot.organization_id,
            actor_user_id=world.owner.id,
            space_id=world.other_space.id,
            targets=[(doc.id, 2)],
            approve=True,
        )


async def test_an_unknown_version_is_not_found(db_session, world, infra):
    doc = await _setup(db_session, world)
    with pytest.raises(NotFoundError):
        await _decide(db_session, world, doc, 9)


async def test_a_version_cannot_be_approved_into_an_archived_document(db_session, world, infra):
    doc = await _setup(db_session, world)
    await _submit(db_session, world, world.editor, doc)
    await db_session.execute(
        update(Document).where(Document.id == doc.id).values(status="archived")
    )
    await db_session.commit()
    with pytest.raises(InvalidInputError, match="archived"):
        await _decide(db_session, world, doc, 2)


async def test_a_version_identical_to_another_document_cannot_be_approved(db_session, world, infra):
    doc = await _setup(db_session, world)
    await _submit(db_session, world, world.editor, doc, V2)
    await _upload(db_session, world, world.admin, "later.md", V2)  # the same bytes, uploaded after

    with pytest.raises(InvalidInputError, match="later.md"):
        await _decide(db_session, world, doc, 2)
    assert (await _versions(db_session, doc.id))[-1].review_state == "pending"


async def test_several_versions_are_decided_together_or_not_at_all(db_session, world, infra):
    first = await _setup(db_session, world)
    second = await _upload(db_session, world, world.admin, "second.md", V3)
    await _submit(db_session, world, world.editor, first, V2)
    await _submit(db_session, world, world.editor, second, V4)
    await db_session.commit()

    with pytest.raises(NotFoundError):
        await version_review.decide(
            db_session,
            organization_id=world.boot.organization_id,
            actor_user_id=world.admin.id,
            space_id=world.space.id,
            targets=[(first.id, 2), (second.id, 7)],
            approve=True,
        )
    assert [v.review_state for v in await _versions(db_session, first.id)] == [
        "approved",
        "pending",
    ]
    changed = await version_review.decide(
        db_session,
        organization_id=world.boot.organization_id,
        actor_user_id=world.admin.id,
        space_id=world.space.id,
        targets=[(first.id, 2), (second.id, 2)],
        approve=True,
    )
    assert set(changed) == {(first.id, 2), (second.id, 2)}


# --------------------------------------------------------------------------
# The reviewers' lists
# --------------------------------------------------------------------------


async def test_reviewers_list_what_waits_and_what_was_refused(db_session, world, infra):
    doc = await _setup(db_session, world)
    await _submit(db_session, world, world.editor, doc, V2, note="10 days")
    await db_session.commit()

    async def listed(state, who=None):
        return await version_review.list_for_review(
            db_session,
            organization_id=world.boot.organization_id,
            actor_user_id=(who or world.admin).id,
            space_id=world.space.id,
            state=state,
        )

    items, total = await listed("pending")
    assert total == 1
    (item,) = items
    assert (item.document_id, item.version_number, item.note) == (doc.id, 2, "10 days")
    assert item.uploader_name == world.editor.display_name
    assert await listed("rejected") == ([], 0)

    await _decide(db_session, world, doc, 2, approve=False, note="no")
    assert (await listed("pending")) == ([], 0)
    refused, _ = await listed("rejected")
    assert [(i.version_number, i.review_note, i.reviewed_by_name) for i in refused] == [
        (2, "no", world.admin.display_name),
    ]
    for who in (world.editor, world.viewer):
        with pytest.raises(ForbiddenError):
            await listed("pending", who)


async def test_a_waiting_version_counts_in_the_reviewers_inbox(db_session, world, infra):
    doc = await _setup(db_session, world)
    await db_session.commit()
    inbox = await review.pending_inbox(
        db_session, organization_id=world.boot.organization_id, user_id=world.admin.id
    )
    assert inbox == []

    await _submit(db_session, world, world.editor, doc)
    await db_session.commit()
    inbox = await review.pending_inbox(
        db_session, organization_id=world.boot.organization_id, user_id=world.admin.id
    )
    assert [(s.space_name, s.pending) for s in inbox] == [("Wiki", 1)]
    # It is counted beside a document waiting in the same space.
    pending_doc = await _upload(db_session, world, world.editor, "new.md", V3)
    assert (await _row(db_session, pending_doc.id)).review_state == "pending"
    inbox = await review.pending_inbox(
        db_session, organization_id=world.boot.organization_id, user_id=world.admin.id
    )
    assert [(s.space_name, s.pending) for s in inbox] == [("Wiki", 2)]


async def test_deleting_the_document_removes_the_file_of_a_waiting_version_too(
    db_session, world, infra
):
    from quberi_api.services import documents as documents_service

    doc = await _setup(db_session, world)
    v2 = await _submit(db_session, world, world.editor, doc)
    await db_session.commit()

    await documents_service.delete_document(
        db_session,
        document_id=doc.id,
        organization_id=world.boot.organization_id,
        space_id=world.space.id,
        caller_user_id=world.admin.id,
    )

    assert v2.storage_key in infra.removed
    assert await _versions(db_session, doc.id) == []


# --------------------------------------------------------------------------
# Over HTTP
# --------------------------------------------------------------------------


async def test_the_web_api_submits_lists_and_decides(db_session, world, infra, blobs, web):
    from quberi_api.api.v1.spaces import document_review

    web._transport.app.include_router(document_review.document_review_router, prefix="/api/v1")
    doc = await _setup(db_session, world)
    base = f"/api/v1/spaces/{world.space.id}/documents/{doc.id}"

    await _as(web, db_session, world, world.editor)
    sent = await web.post(f"{base}/versions", files=_file(V2), data={"note": "10 days"})
    assert sent.status_code == 201, sent.text
    body = sent.json()
    # The document still shows its first file; the response says one is waiting.
    assert (body["version_number"], body["pending_version_number"]) == (1, 2)
    assert body["checksum_sha256"] != ""

    mine = (await web.get(f"{base}/versions")).json()["versions"]
    assert [(v["version_number"], v["review_state"], v["is_current"]) for v in mine] == [
        (2, "pending", False),
        (1, "approved", True),
    ]
    again = await web.post(f"{base}/versions", files=_file(V3))
    assert again.status_code == 409 and "waiting for approval" in again.json()["detail"]
    # (A refused request rolls the test's one shared session back; a real request has its own.)
    await _as(web, db_session, world, world.editor)
    assert (await web.get(base)).json()["pending_version_number"] == 2

    await _as(web, db_session, world, world.viewer)
    assert [
        v["version_number"] for v in (await web.get(f"{base}/versions")).json()["versions"]
    ] == [1]
    assert (await web.get(f"{base}/versions/2/download")).status_code == 404
    assert (await web.get(base)).json()["pending_version_number"] is None
    page = (await web.get(f"/api/v1/spaces/{world.space.id}/documents")).json()
    assert page["items"][0]["pending_version_number"] is None

    root = f"/api/v1/spaces/{world.space.id}/review/versions"
    assert (await web.get(root)).status_code == 403

    await _as(web, db_session, world, world.admin)
    listed = (await web.get(root)).json()
    assert [(i["document_id"], i["version_number"], i["note"]) for i in listed["items"]] == [
        (str(doc.id), 2, "10 days"),
    ]
    page = (await web.get(f"/api/v1/spaces/{world.space.id}/documents")).json()
    assert page["items"][0]["pending_version_number"] == 2
    asked = {"versions": [{"document_id": str(doc.id), "version_number": 2}]}
    assert (await web.post(f"{root}/decide", json={**asked, "action": "reject"})).status_code == 422
    await _as(web, db_session, world, world.admin)
    done = await web.post(f"{root}/decide", json={**asked, "action": "approve", "note": "ok"})
    assert done.status_code == 200, done.text
    assert done.json()["changed"] == asked["versions"]

    await _as(web, db_session, world, world.viewer)
    assert [
        v["version_number"] for v in (await web.get(f"{base}/versions")).json()["versions"]
    ] == [
        2,
        1,
    ]
    now = (await web.get(base)).json()
    assert (now["version_number"], now["version_count"], now["pending_version_number"]) == (
        2,
        2,
        None,
    )

    await _as(web, db_session, world, world.editor)
    refused = await web.post(f"{root}/decide", json={**asked, "action": "approve"})
    assert refused.status_code == 403


async def test_the_decision_endpoint_rejects_bad_requests(db_session, world, infra, blobs, web):
    from quberi_api.api.v1.spaces import document_review

    web._transport.app.include_router(document_review.document_review_router, prefix="/api/v1")
    root = f"/api/v1/spaces/{world.space.id}/review/versions/decide"
    await _as(web, db_session, world, world.admin)
    target = {"document_id": str(uuid.uuid4()), "version_number": 1}
    assert (await web.post(root, json={"versions": [], "action": "approve"})).status_code == 422
    assert (
        await web.post(
            root, json={"versions": [{**target, "version_number": 0}], "action": "approve"}
        )
    ).status_code == 422
    assert (await web.post(root, json={"versions": [target], "action": "maybe"})).status_code == 422
    assert (
        await web.post(root, json={"versions": [target], "action": "approve", "extra": 1})
    ).status_code == 422
    assert (
        await web.post(root, json={"versions": [target], "action": "approve"})
    ).status_code == 404


def test_one_waiting_version_per_document_is_also_a_database_rule():
    index = next(
        i for i in DocumentVersion.__table__.indexes if i.name == new_version.ONE_PENDING_INDEX
    )
    assert index.unique and [c.name for c in index.columns] == ["document_id"]
