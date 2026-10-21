# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""The same file is not stored twice in one space.

Two uploads are the same file when their SHA-256 matches. The rule is per space; it
counts files that are approved or waiting for approval, not rejected ones; guest
sandbox uploads and reference-only documents are outside it. Whoever uploads is told
which file is already there only if a rule has not hidden it from them.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from quberi_api.api.v1.external_schemas import UploadResponse
from quberi_api.api.v1.spaces import documents as web_documents
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document
from quberi_api.services import document_sources
from quberi_api.services import documents as documents_service
from quberi_api.services.documents import core as documents_core
from quberi_api.services.documents import review
from quberi_api.services.documents.errors import DuplicateDocumentError
from quberi_api.services.errors import InvalidInputError
from quberi_api.services.mcp import formatting
from quberi_api.services.mcp.adapters import AdapterHTTPError
from tests.test_document_acl import _member, _rule
from tests.test_external_save import _auth, _org, _tokens, http, redis_client  # noqa: F401
from tests.test_image_indexing import _space
from tests.test_image_search import _grant

SAME = b"# the quarterly plan\n\nnothing here changes\n"
UPLOAD = "/api/v1/external/upload"
ALL = "knowledge:read knowledge:write knowledge:upload"


@pytest.fixture
def infra(monkeypatch):
    """No MinIO and no queue; remember what was stored and what was thrown away again."""
    seen = SimpleNamespace(stored=[], removed=[], jobs=[])

    async def fake_store(*, storage_key, source_stream, content_type, max_bytes):
        blob = source_stream.read()
        seen.stored.append(storage_key)
        return SimpleNamespace(
            storage_key=storage_key,
            size_bytes=len(blob),
            checksum_sha256=hashlib.sha256(blob).hexdigest(),
        )

    async def fake_delete(storage_key):
        seen.removed.append(storage_key)

    async def fake_enqueue(document_id):
        seen.jobs.append(document_id)
        return "job"

    monkeypatch.setattr(documents_core.local_connector, "store", fake_store)
    monkeypatch.setattr(documents_core.local_connector, "delete", fake_delete)
    monkeypatch.setattr(documents_core, "enqueue_index_document", fake_enqueue)
    import quberi_api.workers.enqueue as enqueue_module

    monkeypatch.setattr(enqueue_module, "enqueue_index_document", fake_enqueue)
    return seen


@pytest.fixture
async def world(db_session, org_and_owner):
    db, boot = db_session, org_and_owner
    space = SimpleNamespace(id=(await _space(db, boot, "Wiki")).id)
    elsewhere = SimpleNamespace(id=(await _space(db, boot, "Elsewhere")).id)
    admin, editor, other_editor = [await _member(db, boot) for _ in range(3)]
    for who, role in ((admin, "admin"), (editor, "editor"), (other_editor, "editor")):
        await _grant(db, boot, space, who, role=role)
        await _grant(db, boot, elsewhere, who, role=role)
    return SimpleNamespace(
        boot=boot, space=space, elsewhere=elsewhere, admin=admin, editor=editor,
        other_editor=other_editor,
    )


async def _upload(db, w, who, name, data=SAME, space=None, attributes=None):
    await db.commit()
    view = await documents_service.upload_local(
        db, organization_id=w.boot.organization_id, space_id=(space or w.space).id,
        uploader_user_id=who.id, filename=name, file_obj=io.BytesIO(data),
        content_type="text/markdown", attributes=attributes,
    )
    await db.commit()
    return view.document


async def _count(db, space_id, checksum=None):
    await db.commit()
    stmt = select(Document).where(Document.space_id == space_id)
    if checksum:
        stmt = stmt.where(Document.checksum_sha256 == checksum)
    return len((await db.execute(stmt)).scalars().all())


SAME_SUM = hashlib.sha256(SAME).hexdigest()


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------


async def test_the_same_file_twice_in_one_space_is_refused_and_the_copy_is_thrown_away(
    db_session, world, infra
):
    first = await _upload(db_session, world, world.editor, "plan.md")

    with pytest.raises(DuplicateDocumentError) as excinfo:
        await _upload(db_session, world, world.other_editor, "plan-copy.md")

    err = excinfo.value
    assert err.readable is True
    assert err.document_id == first.id
    assert err.name == "plan.md"
    assert "plan.md" in str(err)
    # Only one row, and the blob that was written for the refused copy was removed again.
    assert await _count(db_session, world.space.id, SAME_SUM) == 1
    assert len(infra.stored) == 2 and len(infra.removed) == 1
    assert infra.removed[0] == infra.stored[1]
    # The attempt is on record: who, which file was already there, never the content.
    await db_session.commit()
    events = (
        await db_session.execute(
            select(AuditEvent).where(AuditEvent.action == "document.upload_duplicate")
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].resource_id == first.id
    assert events[0].payload["existing_document_id"] == str(first.id)
    assert "quarterly" not in str(events[0].payload)


async def test_a_refused_copy_is_not_indexed(db_session, world, infra):
    await _upload(db_session, world, world.editor, "plan.md")
    jobs_before = list(infra.jobs)

    with pytest.raises(DuplicateDocumentError):
        await _upload(db_session, world, world.editor, "again.md")

    assert infra.jobs == jobs_before


async def test_the_same_file_in_another_space_is_a_different_piece_of_knowledge(
    db_session, world, infra
):
    await _upload(db_session, world, world.editor, "plan.md")

    other = await _upload(db_session, world, world.editor, "plan.md", space=world.elsewhere)

    assert other.space_id == world.elsewhere.id


async def test_a_different_file_with_the_same_name_is_fine(db_session, world, infra):
    await _upload(db_session, world, world.editor, "plan.md")

    second = await _upload(db_session, world, world.editor, "plan.md", data=SAME + b"more")

    assert second.name == "plan.md"
    assert await _count(db_session, world.space.id) == 2


async def test_a_file_that_is_waiting_for_approval_counts(db_session, world, infra):
    await db_session.commit()
    await review.set_requirement(
        db_session, organization_id=world.boot.organization_id,
        actor_user_id=world.boot.user_id, space_id=world.space.id, enabled=True,
    )
    waiting = await _upload(db_session, world, world.editor, "plan.md")
    assert waiting.review_state == "pending"

    with pytest.raises(DuplicateDocumentError) as excinfo:
        await _upload(db_session, world, world.other_editor, "plan-again.md")

    assert excinfo.value.review_state == "pending"
    assert "等待审核" in str(excinfo.value) and "waiting for approval" in str(excinfo.value)


async def test_a_rejected_copy_does_not_block_uploading_the_file_again(
    db_session, world, infra
):
    await db_session.commit()
    await review.set_requirement(
        db_session, organization_id=world.boot.organization_id,
        actor_user_id=world.boot.user_id, space_id=world.space.id, enabled=True,
    )
    first = await _upload(db_session, world, world.editor, "plan.md")
    await db_session.commit()
    await review.decide(
        db_session, organization_id=world.boot.organization_id,
        actor_user_id=world.admin.id, space_id=world.space.id,
        document_ids=[first.id], approve=False, note="not this version",
    )

    again = await _upload(db_session, world, world.editor, "plan.md")

    assert again.id != first.id and again.review_state == "pending"


async def test_guest_sandbox_uploads_are_never_refused(db_session, world, infra):
    mine = {"guest_owner": "visitor-one", "guest_expires_at": "2099-01-01T00:00:00+00:00"}
    theirs = {"guest_owner": "visitor-two", "guest_expires_at": "2099-01-01T00:00:00+00:00"}

    await _upload(db_session, world, world.editor, "a.md", attributes=mine)
    await _upload(db_session, world, world.editor, "b.md", attributes=theirs)

    assert await _count(db_session, world.space.id, SAME_SUM) == 2


# --------------------------------------------------------------------------
# What the person is told
# --------------------------------------------------------------------------


async def test_a_file_hidden_from_the_uploader_is_not_named(db_session, world, infra):
    secret = await _upload(db_session, world, world.admin, "secret-merger-plan.md")
    await _rule(
        db_session, world.boot, world.space, secret, "deny", "user", world.editor.id
    )

    with pytest.raises(DuplicateDocumentError) as excinfo:
        await _upload(db_session, world, world.editor, "my-copy.md")

    err = excinfo.value
    assert err.readable is False
    assert err.document_id is None and err.name is None and err.review_state is None
    assert "secret-merger-plan" not in str(err)
    # It is still refused: two copies would break the one-copy rule for everyone else.
    assert await _count(db_session, world.space.id, SAME_SUM) == 1


# --------------------------------------------------------------------------
# Approving a rejected copy
# --------------------------------------------------------------------------


async def _reject(db, w, doc):
    await db.commit()
    await review.decide(
        db, organization_id=w.boot.organization_id, actor_user_id=w.admin.id,
        space_id=w.space.id, document_ids=[doc.id], approve=False, note="no",
    )


async def test_a_rejected_copy_cannot_be_approved_while_an_identical_file_is_live(
    db_session, world, infra
):
    await db_session.commit()
    await review.set_requirement(
        db_session, organization_id=world.boot.organization_id,
        actor_user_id=world.boot.user_id, space_id=world.space.id, enabled=True,
    )
    old = await _upload(db_session, world, world.editor, "plan.md")
    await _reject(db_session, world, old)
    await _upload(db_session, world, world.editor, "plan-v2.md")  # same bytes, now waiting

    with pytest.raises(InvalidInputError) as excinfo:
        await review.decide(
            db_session, organization_id=world.boot.organization_id,
            actor_user_id=world.admin.id, space_id=world.space.id,
            document_ids=[old.id], approve=True, note="",
        )

    assert "plan-v2.md" in str(excinfo.value)


async def test_two_rejected_copies_of_one_file_cannot_be_approved_together(
    db_session, world, infra
):
    await db_session.commit()
    await review.set_requirement(
        db_session, organization_id=world.boot.organization_id,
        actor_user_id=world.boot.user_id, space_id=world.space.id, enabled=True,
    )
    one = await _upload(db_session, world, world.editor, "one.md")
    await _reject(db_session, world, one)
    two = await _upload(db_session, world, world.editor, "two.md")
    await _reject(db_session, world, two)

    with pytest.raises(InvalidInputError):
        await review.decide(
            db_session, organization_id=world.boot.organization_id,
            actor_user_id=world.admin.id, space_id=world.space.id,
            document_ids=[one.id, two.id], approve=True, note="",
        )


# --------------------------------------------------------------------------
# The index behind the rule (two uploads that arrive together)
# --------------------------------------------------------------------------


async def _raw_doc(db, w, name, *, checksum, storage_key="set", status="uploaded",
                   review_state="approved", attributes=None):
    local = await document_sources.ensure_local_source(db, organization_id=w.boot.organization_id)
    await db.commit()
    doc = Document(
        organization_id=w.boot.organization_id, space_id=w.space.id, source_id=local.id,
        name=name, mime_type="text/markdown", size_bytes=1,
        storage_key=f"{w.space.id}/{uuid.uuid4()}/{name}" if storage_key == "set" else storage_key,
        checksum_sha256=checksum, external_ref={}, status=status, review_state=review_state,
        attributes=attributes or {},
    )
    db.add(doc)
    await db.flush()
    await db.commit()
    return doc


async def test_the_database_itself_refuses_a_second_live_copy(db_session, world):
    await _raw_doc(db_session, world, "a.md", checksum=SAME_SUM)

    with pytest.raises(IntegrityError) as excinfo:
        await _raw_doc(db_session, world, "b.md", checksum=SAME_SUM)

    assert "uq_documents_space_checksum_live" in str(excinfo.value)
    await db_session.rollback()


async def test_the_index_leaves_out_what_the_rule_leaves_out(db_session, world):
    await _raw_doc(db_session, world, "live.md", checksum=SAME_SUM)
    # None of these count, so none of them collide with the live copy (or each other).
    await _raw_doc(db_session, world, "rejected-1.md", checksum=SAME_SUM, review_state="rejected")
    await _raw_doc(db_session, world, "rejected-2.md", checksum=SAME_SUM, review_state="rejected")
    await _raw_doc(db_session, world, "archived.md", checksum=SAME_SUM, status="archived")
    await _raw_doc(db_session, world, "reference-only.md", checksum=SAME_SUM, storage_key=None)
    await _raw_doc(db_session, world, "guest.md", checksum=SAME_SUM,
                   attributes={"guest_owner": "visitor"})
    await _raw_doc(db_session, world, "no-checksum-1.md", checksum=None)
    await _raw_doc(db_session, world, "no-checksum-2.md", checksum=None)

    assert await _count(db_session, world.space.id) == 8


# --------------------------------------------------------------------------
# Outside tools and the web
# --------------------------------------------------------------------------


async def _tool_world(db, redis):
    boot = await _org(db)
    space = await _space(db, boot, "Wiki")
    space_id = space.id
    users = [await _member(db, boot) for _ in range(2)]
    user_ids = [u.id for u in users]
    for uid in user_ids:
        await _grant(db, boot, SimpleNamespace(id=space_id), SimpleNamespace(id=uid), role="editor")
    tokens = [await _tokens(db, redis, boot, scope=ALL, user_id=uid) for uid in user_ids]
    await db.commit()
    return boot, SimpleNamespace(id=space_id), user_ids, tokens


@pytest.fixture
def tool_infra(monkeypatch):
    removed = []

    async def fake_store(*, storage_key, source_stream, content_type, max_bytes):
        blob = source_stream.read()
        return SimpleNamespace(
            storage_key=storage_key, size_bytes=len(blob),
            checksum_sha256=hashlib.sha256(blob).hexdigest(),
        )

    async def fake_delete(storage_key):
        removed.append(storage_key)

    async def fake_enqueue(document_id):
        return "job"

    monkeypatch.setattr(documents_core.local_connector, "store", fake_store)
    monkeypatch.setattr(documents_core.local_connector, "delete", fake_delete)
    monkeypatch.setattr(documents_core, "enqueue_index_document", fake_enqueue)
    return removed


def _file(name, data=SAME):
    return {"file": (name, data, "text/markdown")}


async def test_an_outside_tool_is_told_the_file_is_already_there(
    http, db_session, redis_client, tool_infra
):
    boot, space, _users, tokens = await _tool_world(db_session, redis_client)
    first = await http.post(UPLOAD, headers=_auth(tokens[0]), files=_file("plan.md"),
                            data={"space": "Wiki"})
    assert first.status_code == 201, first.text
    assert first.json()["duplicate"] is False

    again = await http.post(UPLOAD, headers=_auth(tokens[1]), files=_file("copy.md"),
                            data={"space": "Wiki"})

    # An answer, not an error, and not "created".
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["duplicate"] is True
    assert body["document_id"] == first.json()["document_id"]
    assert body["name"] == "plan.md"
    assert len(tool_infra) == 1  # the refused copy was removed from storage
    assert await _count(db_session, space.id, SAME_SUM) == 1
    await db_session.commit()
    actions = (await db_session.execute(select(AuditEvent.action))).scalars().all()
    assert "knowledge.external_upload_duplicate" in actions


async def test_an_outside_tool_is_not_told_the_name_of_a_file_it_cannot_read(
    http, db_session, redis_client, tool_infra
):
    boot, space, users, tokens = await _tool_world(db_session, redis_client)
    first = await http.post(UPLOAD, headers=_auth(tokens[0]),
                            files=_file("board-minutes.md"), data={"space": "Wiki"})
    assert first.status_code == 201, first.text
    await _rule(
        db_session, boot, space, SimpleNamespace(id=uuid.UUID(first.json()["document_id"])),
        "deny", "user", users[1],
    )

    refused = await http.post(UPLOAD, headers=_auth(tokens[1]), files=_file("mine.md"),
                              data={"space": "Wiki"})

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "already_exists"
    assert "board-minutes" not in refused.text


def test_the_tool_answer_says_not_uploaded_and_names_the_existing_file():
    result = UploadResponse(
        document_id=uuid.uuid4(), space_id=uuid.uuid4(), space_name="Wiki", name="plan.md",
        mime_type="text/markdown", size_bytes=10, review_state="approved", duplicate=True,
    )

    text = formatting.format_upload_result(result)

    assert text.startswith("NOT uploaded")
    assert '"plan.md"' in text and "Wiki" in text
    assert "indexed" not in text  # not the "still being indexed" message


def test_the_tool_answer_for_a_waiting_original_says_so():
    result = UploadResponse(
        document_id=uuid.uuid4(), space_id=uuid.uuid4(), space_name="Wiki", name="plan.md",
        mime_type="text/markdown", size_bytes=10, review_state="pending", duplicate=True,
    )

    assert "waiting for approval" in formatting.format_upload_result(result)


def test_a_refusal_for_a_hidden_file_is_worded_as_not_uploaded():
    text = formatting.format_error(
        AdapterHTTPError(409, {"code": "already_exists", "message": "identical content."})
    )

    assert text.startswith("NOT uploaded")


def test_the_web_answers_409_with_the_bilingual_text():
    exc = DuplicateDocumentError("这个空间里已有相同的文件。 This space already has it.", readable=True)

    http_exc = web_documents._translate(exc)

    assert http_exc.status_code == 409
    assert "This space already has it" in http_exc.detail
