"""Upload approval: a space can ask that uploads are approved before they are indexed."""

from __future__ import annotations

import io
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document
from quberi_api.domain.spaces.models import Space
from quberi_api.services import documents as documents_service
from quberi_api.services import feishu_sources
from quberi_api.services.documents import core as documents_core
from quberi_api.services.documents import review
from quberi_api.services.errors import ForbiddenError, InvalidInputError, NotFoundError
from quberi_api.services.rag import image_backfill, indexer
from tests.test_document_acl import _member
from tests.test_image_indexing import _space
from tests.test_image_search import _grant


@pytest.fixture
def infra(monkeypatch):
    """No MinIO and no queue: record what would have been stored and indexed."""
    jobs: list[uuid.UUID] = []

    async def fake_store(*, storage_key, source_stream, content_type, max_bytes):
        blob = source_stream.read()
        return SimpleNamespace(
            storage_key=storage_key, size_bytes=len(blob), checksum_sha256="0" * 64
        )

    async def fake_enqueue(document_id):
        jobs.append(document_id)
        return "job"

    monkeypatch.setattr(documents_core.local_connector, "store", fake_store)
    monkeypatch.setattr(documents_core, "enqueue_index_document", fake_enqueue)
    import quberi_api.workers.enqueue as enqueue_module

    monkeypatch.setattr(enqueue_module, "enqueue_index_document", fake_enqueue)
    return jobs


@pytest.fixture
async def world(db_session, org_and_owner):
    db, boot = db_session, org_and_owner
    space = SimpleNamespace(id=(await _space(db, boot, "Wiki")).id)
    owner = SimpleNamespace(id=boot.user_id)
    admin, editor, other_editor, viewer = [await _member(db, boot) for _ in range(4)]
    await _grant(db, boot, space, admin, role="admin")
    await _grant(db, boot, space, editor, role="editor")
    await _grant(db, boot, space, other_editor, role="editor")
    await _grant(db, boot, space, viewer, role="viewer")
    return SimpleNamespace(boot=boot, space=space, owner=owner, admin=admin, editor=editor,
                           other_editor=other_editor, viewer=viewer)


async def _require(db, w, enabled=True, actor=None):
    await db.commit()
    await review.set_requirement(
        db, organization_id=w.boot.organization_id, actor_user_id=(actor or w.owner).id,
        space_id=w.space.id, enabled=enabled,
    )


async def _upload(db, w, who, name="plan.md"):
    await db.commit()
    view = await documents_service.upload_local(
        db, organization_id=w.boot.organization_id, space_id=w.space.id,
        uploader_user_id=who.id, filename=name, file_obj=io.BytesIO(b"# hello"),
        content_type="text/markdown",
    )
    await db.commit()
    return SimpleNamespace(id=view.document.id, state=view.document.review_state)


async def _names(db, w, who):
    await db.commit()
    page = await documents_service.list_documents_in_space(
        db, organization_id=w.boot.organization_id, space_id=w.space.id, caller_user_id=who.id
    )
    await db.commit()
    return sorted(v.document.name for v in page.items), page.total


async def _can_open(db, w, who, doc) -> bool:
    await db.commit()
    try:
        await documents_service.get_document(
            db, document_id=doc.id, organization_id=w.boot.organization_id,
            space_id=w.space.id, caller_user_id=who.id,
        )
        return True
    except NotFoundError:
        return False
    finally:
        await db.commit()


async def _decide(db, w, actor, ids, approve=True, note=""):
    await db.commit()
    return await review.decide(
        db, organization_id=w.boot.organization_id, actor_user_id=actor.id,
        space_id=w.space.id, document_ids=ids, approve=approve, note=note,
    )


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


async def test_only_an_owner_can_turn_it_on_and_it_is_audited(db_session, world):
    db, w = db_session, world
    for who in (w.admin, w.editor, w.viewer):
        await db.commit()
        with pytest.raises(ForbiddenError):
            await review.set_requirement(
                db, organization_id=w.boot.organization_id, actor_user_id=who.id,
                space_id=w.space.id, enabled=True,
            )
        await db.rollback()
    stranger = await _member(db, w.boot)
    await db.commit()
    with pytest.raises(NotFoundError):
        await review.set_requirement(
            db, organization_id=w.boot.organization_id, actor_user_id=stranger.id,
            space_id=w.space.id, enabled=True,
        )
    await db.rollback()
    with pytest.raises(InvalidInputError):  # a personal space has nobody to approve
        await review.set_requirement(
            db, organization_id=w.boot.organization_id, actor_user_id=w.owner.id,
            space_id=w.boot.personal_space_id, enabled=True,
        )
    await db.rollback()

    await _require(db, w, True)
    row = await db.get(Space, w.space.id)
    assert review.space_requires_review(row.attributes) is True
    await _require(db, w, False)
    await db.refresh(row)
    assert review.space_requires_review(row.attributes) is False
    actions = (
        await db.execute(select(AuditEvent.payload).where(AuditEvent.action == "space.upload_review_changed"))
    ).scalars().all()
    assert sorted(p["enabled"] for p in actions) == [False, True]


# ---------------------------------------------------------------------------
# Uploading
# ---------------------------------------------------------------------------


async def test_an_editors_upload_waits_and_is_not_indexed(db_session, world, infra):
    db, w = db_session, world
    await _require(db, w)
    mine = await _upload(db, w, w.editor, "mine.md")
    assert mine.state == "pending"
    assert infra == []  # nothing queued for indexing


async def test_owners_and_admins_upload_straight_in(db_session, world, infra):
    db, w = db_session, world
    await _require(db, w)
    a = await _upload(db, w, w.owner, "owner.md")
    b = await _upload(db, w, w.admin, "admin.md")
    assert (a.state, b.state) == ("approved", "approved")
    assert infra == [a.id, b.id]


async def test_without_the_setting_everything_is_approved_as_before(db_session, world, infra):
    db, w = db_session, world
    doc = await _upload(db, w, w.editor)
    assert doc.state == "approved" and infra == [doc.id]


# ---------------------------------------------------------------------------
# Who sees a waiting upload
# ---------------------------------------------------------------------------


async def test_a_waiting_upload_is_seen_only_by_its_uploader_and_the_reviewers(
    db_session, world, infra
):
    db, w = db_session, world
    await _require(db, w)
    doc = await _upload(db, w, w.editor, "draft.md")

    for who in (w.editor, w.admin, w.owner):
        assert await _names(db, w, who) == (["draft.md"], 1)
        assert await _can_open(db, w, who, doc) is True
    for who in (w.other_editor, w.viewer):
        assert await _names(db, w, who) == ([], 0)
        assert await _can_open(db, w, who, doc) is False
    stats = await documents_service.stats_for_space(
        db, organization_id=w.boot.organization_id, space_id=w.space.id,
        caller_user_id=w.viewer.id,
    )
    assert stats.total == 0


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


async def test_approving_queues_indexing_and_opens_it_to_everyone(db_session, world, infra):
    db, w = db_session, world
    await _require(db, w)
    doc = await _upload(db, w, w.editor, "draft.md")
    changed = await _decide(db, w, w.admin, [doc.id], True, "looks fine")
    assert changed == [doc.id] and infra == [doc.id]
    row = await db.get(Document, doc.id)
    assert (row.review_state, row.review_note, row.reviewed_by_user_id) == ("approved", "looks fine", w.admin.id)
    assert await _names(db, w, w.viewer) == (["draft.md"], 1)
    actions = (
        await db.execute(select(AuditEvent.action).where(AuditEvent.action == "document.review_approved"))
    ).scalars().all()
    assert actions == ["document.review_approved"]


async def test_rejecting_needs_a_reason_keeps_the_record_and_can_be_reversed(
    db_session, world, infra
):
    db, w = db_session, world
    await _require(db, w)
    doc = await _upload(db, w, w.editor, "draft.md")
    with pytest.raises(InvalidInputError):
        await _decide(db, w, w.owner, [doc.id], False, "  ")
    await db.rollback()

    await _decide(db, w, w.owner, [doc.id], False, "contains customer data")
    assert infra == []
    row = await db.get(Document, doc.id)
    assert (row.review_state, row.review_note) == ("rejected", "contains customer data")
    # The uploader still sees it, with the reason; others do not.
    assert await _names(db, w, w.editor) == (["draft.md"], 1)
    assert await _names(db, w, w.viewer) == ([], 0)
    items, total = await review.list_for_review(
        db, organization_id=w.boot.organization_id, actor_user_id=w.owner.id,
        space_id=w.space.id, state="rejected",
    )
    assert total == 1 and items[0].review_note == "contains customer data"
    await db.commit()
    # A refusal can be reversed.
    await _decide(db, w, w.owner, [doc.id], True)
    assert infra == [doc.id]
    # An approved one cannot be decided again.
    with pytest.raises(InvalidInputError):
        await _decide(db, w, w.owner, [doc.id], False, "too late")
    await db.rollback()


async def test_only_reviewers_decide_and_a_batch_is_all_or_nothing(db_session, world, infra):
    db, w = db_session, world
    await _require(db, w)
    one = await _upload(db, w, w.editor, "one.md")
    two = await _upload(db, w, w.other_editor, "two.md")
    for who in (w.editor, w.viewer):
        with pytest.raises(ForbiddenError):  # even the uploader cannot approve their own
            await _decide(db, w, who, [one.id])
        await db.rollback()
    with pytest.raises(NotFoundError):  # one id is not in this space
        await _decide(db, w, w.owner, [one.id, uuid.uuid4()])
    await db.rollback()
    assert (await db.get(Document, one.id)).review_state == "pending"

    changed = await _decide(db, w, w.admin, [one.id, two.id], True)
    assert sorted(changed) == sorted([one.id, two.id])
    pending, total = await review.list_for_review(
        db, organization_id=w.boot.organization_id, actor_user_id=w.admin.id, space_id=w.space.id
    )
    assert total == 0 and pending == []
    await db.commit()
    with pytest.raises(ForbiddenError):  # the waiting list is for reviewers
        await review.list_for_review(
            db, organization_id=w.boot.organization_id, actor_user_id=w.editor.id, space_id=w.space.id
        )
    await db.rollback()


async def test_turning_the_setting_off_does_not_release_what_is_waiting(db_session, world, infra):
    db, w = db_session, world
    await _require(db, w)
    doc = await _upload(db, w, w.editor)
    await _require(db, w, False)
    assert (await db.get(Document, doc.id)).review_state == "pending"
    later = await _upload(db, w, w.editor, "later.md")
    assert later.state == "approved"


async def test_the_inbox_counts_only_spaces_the_person_reviews(db_session, world, infra):
    db, w = db_session, world
    await _require(db, w)
    await _upload(db, w, w.editor, "a.md")
    await _upload(db, w, w.other_editor, "b.md")
    await db.commit()
    mine = await review.pending_inbox(db, organization_id=w.boot.organization_id, user_id=w.admin.id)
    assert [(s.space_name, s.pending) for s in mine] == [("Wiki", 2)]
    await db.commit()
    assert await review.pending_inbox(db, organization_id=w.boot.organization_id, user_id=w.editor.id) == []


# ---------------------------------------------------------------------------
# Nothing waiting is ever indexed
# ---------------------------------------------------------------------------


async def test_the_indexer_refuses_a_document_that_is_not_approved(db_session, world, infra):
    db, w = db_session, world
    await _require(db, w)
    doc = await _upload(db, w, w.editor)
    with pytest.raises(indexer.ReviewPendingError):
        await indexer._run_pipeline(db, qdrant=None, document_id=doc.id)  # type: ignore[arg-type]
    await db.rollback()

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    out = await indexer.index_document({"session_factory": _Factory(), "qdrant": None}, str(doc.id))
    assert out == {"document_id": str(doc.id), "skipped": "not_approved"}
    with pytest.raises(indexer.ReviewPendingError):
        await indexer.index_document_from_text(db, qdrant=None, document_id=doc.id, text="x")  # type: ignore[arg-type]


async def test_the_picture_backfill_skips_what_is_waiting(db_session, world, infra):
    db, w = db_session, world
    await _require(db, w)
    await db.commit()
    for name, who in (("waiting.png", w.editor), ("fine.png", w.admin)):
        await db.commit()
        await documents_service.upload_local(
            db, organization_id=w.boot.organization_id, space_id=w.space.id,
            uploader_user_id=who.id, filename=name, file_obj=io.BytesIO(b"\x89PNG\r\n\x1a\n"),
            content_type="image/png",
        )
    await db.commit()
    ids = await image_backfill.pictures_needing_index(db, model_id="siglip2-base")
    names = (await db.execute(select(Document.name).where(Document.id.in_(ids)))).scalars().all()
    assert names == ["fine.png"]


async def test_connecting_a_feishu_folder_to_a_reviewed_space_needs_a_reviewer(
    db_session, world, infra
):
    db, w = db_session, world
    await _require(db, w)
    config = {"folder_token": "fldcnABCDEFGH12345", "target_space_id": str(w.space.id)}
    await db.commit()
    with pytest.raises(ForbiddenError):
        await feishu_sources.prepare_config(
            db, organization_id=w.boot.organization_id, actor_user_id=w.editor.id, config=config
        )
    await db.rollback()
    clean, _token = await feishu_sources.prepare_config(
        db, organization_id=w.boot.organization_id, actor_user_id=w.admin.id, config=config
    )
    assert clean["target_space_id"] == str(w.space.id)
    await db.commit()
    await _require(db, w, False)
    await feishu_sources.prepare_config(
        db, organization_id=w.boot.organization_id, actor_user_id=w.editor.id, config=config
    )
