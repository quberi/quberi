"""Saving a text note into a space (the write side used by external agents).

``documents.save_note`` is a thin wrapper over ``upload_local``: it composes
the note into a markdown blob and pushes it through the normal upload path, so
a saved note is indistinguishable from an uploaded ``.md`` file downstream.

These tests pin the behaviour ``save_note`` adds on top of ``upload_local``:

* the editor+ permission check is inherited (a viewer is refused);
* empty title / empty text are rejected before any storage work;
* oversize text is rejected;
* the composed markdown carries the title, source provenance and the note
  body, and the stored document is a ``text/markdown`` file named ``<title>.md``
  tagged with ``origin='note'``.

The MinIO store and the async index enqueue are the two bits of infrastructure
``upload_local`` touches that the test environment doesn't run, so both are
monkeypatched. Everything else — the Slice-B permission evaluator, the Document
row, the audit event — runs for real against the test database.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

import pytest

from quberi_api.services import documents as documents_service
from quberi_api.services import members as members_service
from quberi_api.services import space_bindings as space_bindings_service
from quberi_api.services import spaces as spaces_service
from quberi_api.services.documents import core as documents_core
from quberi_api.services.documents.core import _compose_note_markdown
from quberi_api.services.documents.errors import (
    DocumentPermissionError,
    DocumentValidationError,
)

# ---------------------------------------------------------------------------
# Helpers (same shape as the Feishu guard tests)
# ---------------------------------------------------------------------------


async def _space(db, org, name="Notes", slug=None):
    await db.commit()
    view = await spaces_service.create_collaborative_space(
        db,
        organization_id=org.organization_id,
        creator_user_id=org.user_id,
        name=name,
        slug=slug or f"s-{uuid.uuid4().hex[:8]}",
    )
    return view.space


async def _member(db, org, role_code="member"):
    await db.commit()
    view = await members_service.invite_member(
        db,
        organization_id=org.organization_id,
        invited_by_user_id=org.user_id,
        email=f"{role_code}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=role_code.title(),
        role_code=role_code,
    )
    return view.user


async def _grant(db, org, space, user, role):
    await db.commit()
    await space_bindings_service.add_binding(
        db,
        space_id=space.id,
        organization_id=org.organization_id,
        actor_user_id=org.user_id,
        subject_type="user",
        subject_id=user.id,
        role_code=role,
    )


@dataclass
class _StoreResult:
    storage_key: str
    size_bytes: int
    checksum_sha256: str


@pytest.fixture
def _no_infra(monkeypatch):
    """Stub out the two infra calls ``upload_local`` makes.

    ``local_connector.store`` would write to MinIO; ``enqueue_index_document``
    would hit Redis. We record what the store was handed so a test can assert
    on the composed markdown that actually travelled through the upload path.
    """

    captured: dict = {}

    async def fake_store(*, storage_key, source_stream, content_type, max_bytes):
        blob = source_stream.read()
        captured["blob"] = blob
        captured["content_type"] = content_type
        captured["storage_key"] = storage_key
        return _StoreResult(
            storage_key=storage_key,
            size_bytes=len(blob),
            checksum_sha256=hashlib.sha256(blob).hexdigest(),
        )

    async def fake_enqueue(document_id):
        captured["enqueued"] = document_id
        return "job-test"

    monkeypatch.setattr(documents_core.local_connector, "store", fake_store)
    monkeypatch.setattr(documents_core, "enqueue_index_document", fake_enqueue)
    return captured


# ---------------------------------------------------------------------------
# Markdown composition (pure, no DB)
# ---------------------------------------------------------------------------


def test_compose_markdown_includes_title_provenance_and_body():
    from datetime import datetime, timezone

    md = _compose_note_markdown(
        title="架构方案",
        text="  用三层结构：接入层、业务层、存储层。  ",
        source_label="DeepSeek Harness 对话",
        source_url="https://example.invalid/chat/1",
        saved_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    )
    assert md.startswith("# 架构方案")
    assert "DeepSeek Harness 对话" in md
    assert "https://example.invalid/chat/1" in md
    assert "2026-01-02T03:04:05+00:00" in md
    # Body is present and stripped of its surrounding whitespace.
    assert "用三层结构：接入层、业务层、存储层。" in md
    assert "  用三层结构" not in md


def test_compose_markdown_omits_absent_provenance():
    from datetime import datetime, timezone

    md = _compose_note_markdown(
        title="Note",
        text="body",
        source_label=None,
        source_url=None,
        saved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert "Source:" not in md
    assert "Link:" not in md
    assert "Saved:" in md


# ---------------------------------------------------------------------------
# Permission + validation (DB, infra stubbed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_viewer_cannot_save_a_note(db_session, org_and_owner, _no_infra):
    space = await _space(db_session, org_and_owner)
    viewer = await _member(db_session, org_and_owner)
    await _grant(db_session, org_and_owner, space, viewer, "viewer")
    with pytest.raises(DocumentPermissionError):
        await documents_service.save_note(
            db_session,
            organization_id=org_and_owner.organization_id,
            space_id=space.id,
            author_user_id=viewer.id,
            title="x",
            text="y",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "title,text",
    [("", "body"), ("   ", "body"), ("title", ""), ("title", "   ")],
)
async def test_empty_title_or_text_is_rejected(
    db_session, org_and_owner, _no_infra, title, text
):
    space = await _space(db_session, org_and_owner)
    with pytest.raises(DocumentValidationError):
        await documents_service.save_note(
            db_session,
            organization_id=org_and_owner.organization_id,
            space_id=space.id,
            author_user_id=org_and_owner.user_id,
            title=title,
            text=text,
        )


@pytest.mark.asyncio
async def test_oversize_note_is_rejected(db_session, org_and_owner, _no_infra):
    space = await _space(db_session, org_and_owner)
    huge = "a" * (documents_core.MAX_NOTE_TEXT_BYTES + 10)
    with pytest.raises(DocumentValidationError):
        await documents_service.save_note(
            db_session,
            organization_id=org_and_owner.organization_id,
            space_id=space.id,
            author_user_id=org_and_owner.user_id,
            title="big",
            text=huge,
        )


@pytest.mark.asyncio
async def test_owner_can_save_a_note_and_it_lands_as_markdown(
    db_session, org_and_owner, _no_infra
):
    space = await _space(db_session, org_and_owner)
    view = await documents_service.save_note(
        db_session,
        organization_id=org_and_owner.organization_id,
        space_id=space.id,
        author_user_id=org_and_owner.user_id,
        title="会议结论",
        text="决定采用方案 B。",
        source_label="Harness 对话",
    )
    doc = view.document
    # Stored as a markdown file named after the title.
    assert doc.name == "会议结论.md"
    assert doc.mime_type == "text/markdown"
    assert doc.attributes.get("origin") == "note"
    assert doc.attributes.get("source_label") == "Harness 对话"
    # The blob that reached the (stubbed) MinIO store is the composed markdown.
    blob = _no_infra["blob"].decode("utf-8")
    assert "# 会议结论" in blob
    assert "决定采用方案 B。" in blob
    assert "Harness 对话" in blob
    assert _no_infra["content_type"] == "text/markdown"
    # Indexing was enqueued for the new document.
    assert _no_infra["enqueued"] == doc.id


# ---------------------------------------------------------------------------
# resolve_target_space: name / default resolution (DB, no infra needed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_defaults_to_personal_space(db_session, org_and_owner):
    view = await spaces_service.resolve_target_space(
        db_session,
        organization_id=org_and_owner.organization_id,
        user_id=org_and_owner.user_id,
        name=None,
    )
    assert view.space.id == org_and_owner.personal_space_id
    assert view.space.preset == "personal"


@pytest.mark.asyncio
async def test_resolve_matches_space_name_case_insensitively(db_session, org_and_owner):
    space = await _space(db_session, org_and_owner, name="产品团队")
    view = await spaces_service.resolve_target_space(
        db_session,
        organization_id=org_and_owner.organization_id,
        user_id=org_and_owner.user_id,
        name="  产品团队  ",
    )
    assert view.space.id == space.id


@pytest.mark.asyncio
async def test_resolve_unknown_name_is_not_found(db_session, org_and_owner):
    from quberi_api.services.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await spaces_service.resolve_target_space(
            db_session,
            organization_id=org_and_owner.organization_id,
            user_id=org_and_owner.user_id,
            name="does-not-exist",
        )


# The external-tools HTTP surface (POST /api/v1/external/save) that calls
# save_note is covered end-to-end in test_external_save.py.
