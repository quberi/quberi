"""Older Office files are refused at upload, with what to do, instead of failing to index."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from quberi_api.services import documents as documents_service
from quberi_api.services.documents import core as documents_core
from quberi_api.services.documents.errors import UnsupportedMimeError


@pytest.fixture(autouse=True)
def _no_infra(monkeypatch):
    async def fake_store(*, storage_key, source_stream, content_type, max_bytes):
        blob = source_stream.read()
        return SimpleNamespace(storage_key=storage_key, size_bytes=len(blob), checksum_sha256="0" * 64)

    async def fake_enqueue(document_id):
        return "job"

    monkeypatch.setattr(documents_core.local_connector, "store", fake_store)
    monkeypatch.setattr(documents_core, "enqueue_index_document", fake_enqueue)


async def _upload(db, boot, name, mime):
    await db.commit()
    return await documents_service.upload_local(
        db, organization_id=boot.organization_id, space_id=boot.personal_space_id,
        uploader_user_id=boot.user_id, filename=name, file_obj=io.BytesIO(b"x"), content_type=mime,
    )


@pytest.mark.parametrize(
    ("name", "mime", "modern"),
    [
        ("报价.xls", "application/vnd.ms-excel", ".xlsx"),
        ("合同.doc", "application/msword", ".docx"),
        ("汇报.ppt", "application/vnd.ms-powerpoint", ".pptx"),
        ("renamed.xls", "application/octet-stream", ".xlsx"),  # judged by name too
        ("no-extension", "application/vnd.ms-excel", ".xlsx"),  # and by declared type
    ],
)
async def test_legacy_formats_are_refused_and_the_message_says_what_to_do(
    db_session, org_and_owner, name, mime, modern
):
    with pytest.raises(UnsupportedMimeError) as excinfo:
        await _upload(db_session, org_and_owner, name, mime)
    assert "另存为" in str(excinfo.value) and modern in str(excinfo.value)
    await db_session.rollback()


@pytest.mark.parametrize(
    ("name", "mime"),
    [
        ("报价.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("合同.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("汇报.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("data.csv", "text/csv"),
    ],
)
async def test_current_formats_still_upload(db_session, org_and_owner, name, mime):
    view = await _upload(db_session, org_and_owner, name, mime)
    assert view.document.name == name
