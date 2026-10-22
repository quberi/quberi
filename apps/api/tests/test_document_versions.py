"""A document keeps every file it has held, and says which one a passage comes from.

Real: database, upload and new-version services, permission evaluator, the vector store
(in memory). Stood in: the file store and the job queue.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from types import SimpleNamespace

import pytest
from qdrant_client import AsyncQdrantClient
from sqlalchemy import select, update

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document, DocumentVersion
from quberi_api.services import documents as documents_service
from quberi_api.services.documents import core as documents_core
from quberi_api.services.documents import new_version
from quberi_api.services.documents import versions as versions_service
from quberi_api.services.documents.errors import (
    DocumentPermissionError,
    DocumentValidationError,
    DuplicateDocumentError,
    VersionConflictError,
)
from quberi_api.services.errors import NotFoundError
from quberi_api.services.rag.vector_store import ChunkPoint, VectorStore, _point_id_for
from tests.test_document_acl import _member, _rule
from tests.test_image_indexing import _space
from tests.test_image_search import _grant

V1 = b"# Leave policy\n\nAnnual leave is 5 days.\n"
V2 = b"# Leave policy\n\nAnnual leave is 10 days.\n"
V3 = b"# Leave policy\n\nAnnual leave is 15 days.\n"


def _sum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def infra(monkeypatch):
    """No MinIO and no queue: remember what was stored, removed and queued."""
    seen = SimpleNamespace(stored=[], removed=[], jobs=[], on_store=None)

    async def fake_store(*, storage_key, source_stream, content_type, max_bytes):
        blob = source_stream.read()
        seen.stored.append(storage_key)
        if seen.on_store is not None:
            await seen.on_store()
        return SimpleNamespace(
            storage_key=storage_key, size_bytes=len(blob), checksum_sha256=_sum(blob)
        )

    async def fake_delete(storage_key):
        seen.removed.append(storage_key)

    async def fake_enqueue(document_id):
        seen.jobs.append(document_id)
        return "job"

    monkeypatch.setattr(documents_core.local_connector, "store", fake_store)
    monkeypatch.setattr(documents_core.local_connector, "delete", fake_delete)
    monkeypatch.setattr(documents_core, "enqueue_index_document", fake_enqueue)
    monkeypatch.setattr(new_version, "enqueue_index_document", fake_enqueue)
    import quberi_api.workers.enqueue as enqueue_module

    monkeypatch.setattr(enqueue_module, "enqueue_index_document", fake_enqueue)
    return seen


@pytest.fixture
async def world(db_session, org_and_owner):
    db, boot = db_session, org_and_owner
    space = SimpleNamespace(id=(await _space(db, boot, "Wiki")).id)
    other_space = SimpleNamespace(id=(await _space(db, boot, "Elsewhere")).id)
    owner = SimpleNamespace(id=boot.user_id)
    admin, editor, viewer, stranger = [await _member(db, boot) for _ in range(4)]
    await _grant(db, boot, space, admin, role="admin")
    await _grant(db, boot, space, editor, role="editor")
    await _grant(db, boot, space, viewer, role="viewer")
    await _grant(db, boot, other_space, editor, role="editor")
    return SimpleNamespace(
        boot=boot, space=space, other_space=other_space, owner=owner, admin=admin,
        editor=editor, viewer=viewer, stranger=stranger,
    )


async def _upload(db, w, who, name="leave.md", data=V1, space=None):
    await db.commit()
    view = await documents_service.upload_local(
        db, organization_id=w.boot.organization_id, space_id=(space or w.space).id,
        uploader_user_id=who.id, filename=name, file_obj=io.BytesIO(data),
        content_type="text/markdown",
    )
    await db.commit()
    return SimpleNamespace(id=view.document.id, storage_key=view.document.storage_key)


async def _new_version(db, w, who, doc, data=V2, name="leave.md", note=None, space=None):
    await db.commit()
    view = await new_version.upload_new_version(
        db, organization_id=w.boot.organization_id, space_id=(space or w.space).id,
        document_id=doc.id, uploader_user_id=who.id, filename=name,
        file_obj=io.BytesIO(data), content_type="text/markdown", note=note,
    )
    await db.commit()
    return view.document


async def _versions(db, doc_id):
    await db.commit()
    return (
        (
            await db.execute(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == doc_id)
                .order_by(DocumentVersion.version_number)
            )
        )
        .scalars()
        .all()
    )


async def _row(db, doc_id):
    await db.commit()
    return (await db.execute(select(Document).where(Document.id == doc_id))).scalar_one()


# --------------------------------------------------------------------------
# The first upload
# --------------------------------------------------------------------------


async def test_an_ordinary_upload_is_version_one_and_the_document_points_at_it(
    db_session, world, infra
):
    doc = await _upload(db_session, world, world.editor)

    versions = await _versions(db_session, doc.id)
    row = await _row(db_session, doc.id)
    assert [v.version_number for v in versions] == [1]
    assert row.current_version_id == versions[0].id
    v1 = versions[0]
    assert (v1.storage_key, v1.checksum_sha256, v1.size_bytes) == (
        row.storage_key, row.checksum_sha256, row.size_bytes,
    )
    assert v1.filename == "leave.md" and v1.created_by_user_id == world.editor.id


async def test_uploading_a_file_with_the_same_name_is_a_new_document_never_a_new_version(
    db_session, world, infra
):
    first = await _upload(db_session, world, world.editor, "leave.md", V1)
    second = await _upload(db_session, world, world.editor, "leave.md", V2)

    assert first.id != second.id
    assert len(await _versions(db_session, first.id)) == 1
    assert len(await _versions(db_session, second.id)) == 1


# --------------------------------------------------------------------------
# Adding a version
# --------------------------------------------------------------------------


async def test_a_new_version_is_added_and_the_old_one_is_left_exactly_as_it_was(
    db_session, world, infra
):
    doc = await _upload(db_session, world, world.editor)
    before = (await _versions(db_session, doc.id))[0]
    old = (
        before.id, before.storage_key, before.checksum_sha256, before.size_bytes, before.filename,
    )

    await _new_version(db_session, world, world.editor, doc, V2, note="10 days now")

    versions = await _versions(db_session, doc.id)
    assert [v.version_number for v in versions] == [1, 2]
    v1, v2 = versions
    assert (v1.id, v1.storage_key, v1.checksum_sha256, v1.size_bytes, v1.filename) == old
    assert v2.checksum_sha256 == _sum(V2) and v2.note == "10 days now"
    assert v2.storage_key != v1.storage_key
    # Never overwrites or removes an earlier file.
    assert infra.removed == []
    row = await _row(db_session, doc.id)
    assert row.current_version_id == v2.id
    assert (row.storage_key, row.checksum_sha256) == (v2.storage_key, v2.checksum_sha256)


async def test_a_new_version_stored_under_the_same_name_gets_a_key_of_its_own(
    db_session, world, infra
):
    doc = await _upload(db_session, world, world.editor)
    await _new_version(db_session, world, world.editor, doc, V2)
    await _new_version(db_session, world, world.editor, doc, V3)

    keys = [v.storage_key for v in await _versions(db_session, doc.id)]
    assert len(set(keys)) == 3
    assert keys[1].endswith(f"{doc.id}.v2/leave.md") and keys[2].endswith(f"{doc.id}.v3/leave.md")
    # A sibling folder, never inside the folder of version 1 (where a file called "v2" may be).
    assert "/v2/" not in keys[1]


async def test_the_document_starts_over_for_indexing_and_a_job_is_queued(
    db_session, world, infra
):
    doc = await _upload(db_session, world, world.editor)
    await db_session.execute(
        update(Document)
        .where(Document.id == doc.id)
        .values(
            indexing_state="indexed", chunk_count=4,
            attributes={"pdf_ocr": {"x": 1}, "keep": 1},
        )
    )
    await db_session.commit()
    infra.jobs.clear()

    await _new_version(db_session, world, world.editor, doc, V2)

    row = await _row(db_session, doc.id)
    assert (row.indexing_state, row.chunk_count, row.last_indexed_at) == ("not_indexed", 0, None)
    assert row.attributes == {"keep": 1}  # what was read from the old file is not the new file's
    assert infra.jobs == [doc.id]


async def test_the_name_follows_the_new_file_but_the_old_version_keeps_its_own(
    db_session, world, infra
):
    doc = await _upload(db_session, world, world.editor, "leave-2025.md", V1)

    await _new_version(db_session, world, world.editor, doc, V2, name="leave-2026.md")

    row = await _row(db_session, doc.id)
    v1, v2 = await _versions(db_session, doc.id)
    assert row.name == "leave-2026.md"
    assert (v1.filename, v2.filename) == ("leave-2025.md", "leave-2026.md")


async def test_the_addition_is_audited_with_who_which_version_and_the_checksum(
    db_session, world, infra
):
    doc = await _upload(db_session, world, world.editor)
    await _new_version(db_session, world, world.admin, doc, V2, note="fixed")

    events = (
        await db_session.execute(
            select(AuditEvent).where(AuditEvent.action == "document.version_added")
        )
    ).scalars().all()
    assert len(events) == 1
    event = events[0]
    assert event.actor_id == world.admin.id and event.resource_id == doc.id
    assert event.payload["version_number"] == 2 and event.payload["previous_version_number"] == 1
    assert event.payload["checksum_sha256"] == _sum(V2)
    assert "10 days" not in str(event.payload)  # never the content


async def test_a_file_can_go_back_to_the_bytes_of_an_older_version(db_session, world, infra):
    doc = await _upload(db_session, world, world.editor, data=V1)
    await _new_version(db_session, world, world.editor, doc, V2)

    await _new_version(db_session, world, world.editor, doc, V1)  # a rollback is a new version

    versions = await _versions(db_session, doc.id)
    assert [v.version_number for v in versions] == [1, 2, 3]
    assert versions[0].checksum_sha256 == versions[2].checksum_sha256


# --------------------------------------------------------------------------
# What is refused
# --------------------------------------------------------------------------


async def test_the_same_bytes_as_the_current_version_is_not_a_new_version(
    db_session, world, infra
):
    doc = await _upload(db_session, world, world.editor, data=V1)
    stored_before = len(infra.stored)

    with pytest.raises(VersionConflictError):
        await _new_version(db_session, world, world.editor, doc, V1)

    assert len(await _versions(db_session, doc.id)) == 1
    assert infra.removed == [infra.stored[stored_before]]  # the unused copy is thrown away


async def test_a_file_already_in_the_space_as_another_document_is_refused(
    db_session, world, infra
):
    doc = await _upload(db_session, world, world.editor, "leave.md", V1)
    other = await _upload(db_session, world, world.editor, "other.md", V2)

    with pytest.raises(DuplicateDocumentError) as excinfo:
        await _new_version(db_session, world, world.editor, doc, V2)

    assert excinfo.value.document_id == other.id
    assert len(await _versions(db_session, doc.id)) == 1
    assert len(infra.removed) == 1


async def test_two_new_versions_at_the_same_moment_do_not_share_a_number(
    db_session, world, infra
):
    doc = await _upload(db_session, world, world.editor)

    async def someone_else_gets_in_first():
        db_session.add(
            DocumentVersion(
                id=uuid.uuid4(), document_id=doc.id, version_number=2, filename="x.md",
                mime_type="text/markdown", size_bytes=1, storage_key=f"k/{uuid.uuid4()}",
                checksum_sha256=_sum(b"other"),
            )
        )
        await db_session.commit()

    infra.on_store = someone_else_gets_in_first
    with pytest.raises(VersionConflictError):
        await _new_version(db_session, world, world.editor, doc, V3)

    assert [v.version_number for v in await _versions(db_session, doc.id)] == [1, 2]
    assert infra.removed  # the file stored for the losing attempt is removed


async def test_a_viewer_cannot_add_a_version(db_session, world, infra):
    doc = await _upload(db_session, world, world.editor)
    with pytest.raises(DocumentPermissionError):
        await _new_version(db_session, world, world.viewer, doc, V2)
    assert len(await _versions(db_session, doc.id)) == 1


async def test_someone_with_no_access_to_the_space_cannot_tell_the_document_exists(
    db_session, world, infra
):
    doc = await _upload(db_session, world, world.editor)
    with pytest.raises(NotFoundError):
        await _new_version(db_session, world, world.stranger, doc, V2)


async def test_a_document_hidden_from_the_uploader_cannot_be_given_a_version(
    db_session, world, infra
):
    doc = await _upload(db_session, world, world.admin)
    await _rule(db_session, world.boot, world.space, doc, "deny", "user", world.editor.id)
    with pytest.raises(NotFoundError):
        await _new_version(db_session, world, world.editor, doc, V2)


async def test_a_document_cannot_be_given_a_version_through_another_space(
    db_session, world, infra
):
    doc = await _upload(db_session, world, world.editor)
    with pytest.raises(NotFoundError):
        await _new_version(db_session, world, world.editor, doc, V2, space=world.other_space)


@pytest.mark.parametrize(
    "values",
    [{"review_state": "pending"}, {"review_state": "rejected"}, {"status": "archived"},
     {"storage_key": None}],
)
async def test_only_an_approved_live_document_with_a_stored_file_takes_a_version(
    db_session, world, infra, values
):
    doc = await _upload(db_session, world, world.editor)
    await db_session.execute(update(Document).where(Document.id == doc.id).values(**values))
    await db_session.commit()

    with pytest.raises(DocumentValidationError):
        await _new_version(db_session, world, world.editor, doc, V2)


async def test_a_legacy_office_file_is_refused_as_a_version_too(db_session, world, infra):
    doc = await _upload(db_session, world, world.editor)
    await db_session.commit()
    from quberi_api.services.documents.errors import UnsupportedMimeError

    with pytest.raises(UnsupportedMimeError):
        await new_version.upload_new_version(
            db_session, organization_id=world.boot.organization_id, space_id=world.space.id,
            document_id=doc.id, uploader_user_id=world.editor.id, filename="old.doc",
            file_obj=io.BytesIO(b"x"), content_type="application/msword",
        )


async def test_the_note_has_a_length_limit(db_session, world, infra):
    doc = await _upload(db_session, world, world.editor)
    with pytest.raises(DocumentValidationError):
        await _new_version(db_session, world, world.editor, doc, V2, note="x" * 501)


# --------------------------------------------------------------------------
# Reading the history
# --------------------------------------------------------------------------


async def test_the_history_lists_newest_first_and_marks_the_current_one(
    db_session, world, infra
):
    doc = await _upload(db_session, world, world.editor)
    await _new_version(db_session, world, world.admin, doc, V2)
    await db_session.commit()

    rows = await new_version.list_document_versions(
        db_session, document_id=doc.id, organization_id=world.boot.organization_id,
        space_id=world.space.id, caller_user_id=world.viewer.id,
    )

    assert [(r.version.version_number, r.is_current) for r in rows] == [(2, True), (1, False)]
    assert rows[0].created_by_name == world.admin.display_name


async def test_the_history_is_hidden_from_someone_who_cannot_read_the_document(
    db_session, world, infra
):
    doc = await _upload(db_session, world, world.admin)
    await _rule(db_session, world.boot, world.space, doc, "deny", "user", world.viewer.id)
    await db_session.commit()

    for fn, extra in (
        (new_version.list_document_versions, {}),
        (new_version.open_version_stream, {"version_number": 1}),
    ):
        with pytest.raises(NotFoundError):
            await fn(
                db_session, document_id=doc.id, organization_id=world.boot.organization_id,
                space_id=world.space.id, caller_user_id=world.viewer.id, **extra,
            )


async def test_an_old_version_can_still_be_opened_by_its_own_file(db_session, world, infra):
    doc = await _upload(db_session, world, world.editor)
    await _new_version(db_session, world, world.editor, doc, V2)
    await db_session.commit()

    version, _stream = await new_version.open_version_stream(
        db_session, document_id=doc.id, version_number=1,
        organization_id=world.boot.organization_id, space_id=world.space.id,
        caller_user_id=world.viewer.id,
    )
    assert version.storage_key == doc.storage_key and version.checksum_sha256 == _sum(V1)
    with pytest.raises(NotFoundError):
        await new_version.open_version_stream(
            db_session, document_id=doc.id, version_number=9,
            organization_id=world.boot.organization_id, space_id=world.space.id,
            caller_user_id=world.viewer.id,
        )


async def test_deleting_a_document_removes_the_file_of_every_version(db_session, world, infra):
    doc = await _upload(db_session, world, world.editor)
    await _new_version(db_session, world, world.editor, doc, V2)
    keys = [v.storage_key for v in await _versions(db_session, doc.id)]
    await db_session.commit()

    await documents_service.delete_document(
        db_session, document_id=doc.id, organization_id=world.boot.organization_id,
        space_id=world.space.id, caller_user_id=world.admin.id,
    )

    assert set(keys) <= set(infra.removed)
    assert await _versions(db_session, doc.id) == []


async def test_the_summaries_a_list_needs(db_session, world, infra):
    one = await _upload(db_session, world, world.editor, "a.md", V1)
    two = await _upload(db_session, world, world.editor, "b.md", V2)
    await _new_version(db_session, world, world.editor, two, V3, name="b.md")
    await db_session.commit()

    current = await versions_service.latest_version_numbers(db_session, [one.id, two.id])
    counts = await versions_service.version_count_by_document(db_session, [one.id, two.id])

    assert current == {one.id: 1, two.id: 2}
    assert counts == {one.id: 1, two.id: 2}


# --------------------------------------------------------------------------
# The index keeps every version, labelled
# --------------------------------------------------------------------------

DIM = 4


def _chunk(index, text, **kw):
    return ChunkPoint(
        chunk_index=index, text=text, vector=[1.0, 0.0, 0.0, 0.0], char_start=0,
        char_end=len(text), **kw,
    )


@pytest.fixture
async def store():
    client = AsyncQdrantClient(":memory:")
    yield VectorStore(client, model_id="test-embed", vector_dim=DIM)
    await client.close()


async def _put(store, doc, ver, texts, **kw):
    await store.upsert_document(
        organization_id=ORG, space_id=SPACE, document_id=doc, document_name="leave.md",
        document_checksum=None, version_number=ver,
        chunks=[_chunk(i, t) for i, t in enumerate(texts)], **kw,
    )


ORG, SPACE = uuid.uuid4(), uuid.uuid4()


async def _hits(store, doc=None):
    got = await store.search(
        query_vector=[1.0, 0.0, 0.0, 0.0], space_ids=[SPACE], organization_id=ORG, limit=50,
        document_ids=[doc] if doc else None,
    )
    return sorted((h.version_number, h.text) for h in got)


async def test_both_versions_stay_searchable_each_with_its_own_number(store):
    doc = uuid.uuid4()
    await _put(store, doc, 1, ["5 days"])
    await _put(store, doc, 2, ["10 days"])

    assert await _hits(store) == [(1, "5 days"), (2, "10 days")]


async def test_re_indexing_one_version_replaces_only_that_versions_points(store):
    doc = uuid.uuid4()
    await _put(store, doc, 1, ["5 days", "carry over"])
    await _put(store, doc, 2, ["10 days", "carry over 5", "extra"])

    await _put(store, doc, 2, ["10 days"])  # a re-index that produced fewer chunks

    assert await _hits(store) == [(1, "5 days"), (1, "carry over"), (2, "10 days")]
    await _put(store, doc, 1, ["5 days"])
    assert await _hits(store) == [(1, "5 days"), (2, "10 days")]


async def test_points_indexed_before_versions_existed_are_version_one(store):
    doc = uuid.uuid4()
    client = store._client
    await store.ensure_collection()
    from qdrant_client.http import models as qm

    await client.upsert(
        store.collection,
        points=[
            qm.PointStruct(
                id=_point_id_for(doc, 0),
                vector=[1.0, 0.0, 0.0, 0.0],
                payload={
                    "organization_id": str(ORG), "space_id": str(SPACE), "document_id": str(doc),
                    "document_name": "old", "chunk_index": 0, "text": "legacy",
                    "char_start": 0, "char_end": 6,
                },
            )
        ],
        wait=True,
    )
    assert await _hits(store) == [(1, "legacy")]

    await _put(store, doc, 2, ["new"])  # indexing version 2 must not touch the legacy point
    assert await _hits(store) == [(1, "legacy"), (2, "new")]

    await _put(store, doc, 1, ["legacy again"])  # version 1 replaces it, under the same id
    assert await _hits(store) == [(1, "legacy again"), (2, "new")]


async def test_a_picture_style_replace_drops_every_version_of_the_document(store):
    doc = uuid.uuid4()
    await _put(store, doc, 1, ["a"])
    await _put(store, doc, 2, ["b"])

    await _put(store, doc, 3, ["c"], keep_other_versions=False)

    assert await _hits(store) == [(3, "c")]


async def test_deleting_the_document_removes_every_version_but_one_version_can_go_alone(store):
    doc, other = uuid.uuid4(), uuid.uuid4()
    await _put(store, doc, 1, ["a"])
    await _put(store, doc, 2, ["b"])
    await _put(store, other, 1, ["keep"])

    await store.delete_document(document_id=doc, version_number=2)
    assert await _hits(store) == [(1, "a"), (1, "keep")]

    await store.delete_document(document_id=doc)
    assert await _hits(store) == [(1, "keep")]


def test_version_one_keeps_the_point_ids_it_always_had_and_later_versions_get_their_own():
    doc = uuid.uuid4()
    assert _point_id_for(doc, 3) == _point_id_for(doc, 3, 1)
    assert _point_id_for(doc, 3, 2) != _point_id_for(doc, 3, 1)
    assert _point_id_for(doc, 3, 2) != _point_id_for(doc, 3, 3)


async def test_exact_code_hits_carry_their_version_too(store):
    doc = uuid.uuid4()
    await store.upsert_document(
        organization_id=ORG, space_id=SPACE, document_id=doc, document_name="n",
        document_checksum=None, version_number=2,
        chunks=[_chunk(0, "flange Φ520", codes=("Φ520",))],
    )
    got = await store.search_by_codes(codes=["Φ520"], space_ids=[SPACE], organization_id=ORG)
    assert [(h.version_number, h.text) for h in got] == [(2, "flange Φ520")]


# --------------------------------------------------------------------------
# The migration's backfill
# --------------------------------------------------------------------------


def _migration():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    path = root / "alembic" / "versions" / "0039_document_versions.py"
    spec = importlib.util.spec_from_file_location("migration_0039", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_documents_stored_before_versions_become_version_one_and_the_rest_are_left(
    db_session, world, infra
):
    """Run the migration's own SQL on documents that have no version record."""
    from sqlalchemy import text

    from tests.test_image_indexing import _doc

    stored = await _doc(db_session, world.boot, world.space, "old.pdf", mime="application/pdf")
    external = await _doc(db_session, world.boot, world.space, "page.docx", mime="text/plain")
    await db_session.execute(
        update(Document)
        .where(Document.id == external.id)
        .values(storage_key=None, checksum_sha256=None)
    )
    await db_session.commit()
    assert await _versions(db_session, stored.id) == []

    migration = _migration()
    await db_session.execute(text(migration.BACKFILL_VERSION_ONE))
    await db_session.execute(text(migration.BACKFILL_CURRENT_VERSION))
    await db_session.commit()

    row = await _row(db_session, stored.id)
    (v1,) = await _versions(db_session, stored.id)
    assert v1.version_number == 1 and row.current_version_id == v1.id
    assert (v1.storage_key, v1.checksum_sha256, v1.size_bytes, v1.filename, v1.mime_type) == (
        row.storage_key, row.checksum_sha256, row.size_bytes, row.name, row.mime_type,
    )
    assert v1.created_at == row.created_at
    # A document with no stored file has no version, and nothing else is touched.
    assert await _versions(db_session, external.id) == []
    assert (await _row(db_session, external.id)).current_version_id is None
