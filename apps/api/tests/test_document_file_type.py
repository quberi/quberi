# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""Filtering a document list by kind (images, PDFs, spreadsheets, ...).

The SQL filter must agree with ``_mime_bucket`` (which labels documents in the
stats), otherwise a chip would show a different set than its own count.
"""

from __future__ import annotations

import uuid
import pytest
from sqlalchemy import select

from quberi_api.domain.documents.models import Document
from quberi_api.services import document_sources as sources_service
from quberi_api.services.documents import core as documents_core
from quberi_api.services.documents.errors import DocumentValidationError
from tests.test_external_save import (  # noqa: F401
    _auth,
    _no_infra,
    _org,
    _tokens,
    http,
    redis_client,
)
from tests.test_external_search_pending import _space

DOCUMENTS = "/api/v1/external/documents"

MIMES = [
    "image/png",
    "IMAGE/JPEG",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.presentation",
    "text/plain",
    "text/markdown",
    "application/json",
    "application/zip",
    "application/octet-stream",
    "application/rtf",
]


async def _doc_of(db, boot, space, name, mime):
    local = await sources_service.ensure_local_source(db, organization_id=boot.organization_id)
    await db.commit()
    doc = Document(
        organization_id=boot.organization_id,
        space_id=space.id,
        source_id=local.id,
        name=name,
        mime_type=mime,
        size_bytes=1,
        checksum_sha256=uuid.uuid4().hex * 2,
        external_ref={},
        indexing_state="indexed",
        status="uploaded",
    )
    db.add(doc)
    await db.flush()
    await db.commit()
    return doc


async def test_sql_filter_agrees_with_the_bucket_used_by_the_stats(db_session):
    boot = await _org(db_session)
    space = await _space(db_session, boot, "资料")
    for i, mime in enumerate(MIMES):
        await _doc_of(db_session, boot, space, f"f{i}", mime)

    seen: dict[str, str] = {}
    for kind in documents_core.FILE_TYPES:
        rows = (
            await db_session.execute(
                select(Document.mime_type).where(
                    Document.space_id == space.id,
                    documents_core.file_type_clause(Document.mime_type, kind),
                )
            )
        ).scalars().all()
        for mime in rows:
            assert documents_core._mime_bucket(mime) == kind, (mime, kind)
            assert mime not in seen, f"{mime} matched both {seen[mime]} and {kind}"
            seen[mime] = kind
    # Every document lands in exactly one group.
    assert sorted(seen) == sorted(MIMES)


async def test_an_unknown_kind_is_rejected_not_ignored():
    with pytest.raises(DocumentValidationError):
        documents_core.file_type_clause(Document.mime_type, "videos")


async def test_external_listing_can_be_narrowed_to_one_kind(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    space = await _space(db_session, boot, "图库")
    await _doc_of(db_session, boot, space, "a.png", "image/png")
    await _doc_of(db_session, boot, space, "b.jpg", "image/jpeg")
    await _doc_of(db_session, boot, space, "c.pdf", "application/pdf")
    tokens = await _tokens(db_session, redis_client, boot, scope="knowledge:read")
    await db_session.commit()
    headers = _auth(tokens)

    everything = (await http.get(f"{DOCUMENTS}?space=图库", headers=headers)).json()
    images = (await http.get(f"{DOCUMENTS}?space=图库&type=image", headers=headers)).json()
    pdfs = (await http.get(f"{DOCUMENTS}?space=图库&type=pdf", headers=headers)).json()
    sheets = (await http.get(f"{DOCUMENTS}?space=图库&type=spreadsheet", headers=headers)).json()

    assert everything["total"] == 3 and everything["type"] is None
    assert images["type"] == "image"  # echoed, so a tool can tell the filter was applied
    assert images["total"] == 2
    assert {d["name"] for d in images["documents"]} == {"a.png", "b.jpg"}
    assert [d["name"] for d in pdfs["documents"]] == ["c.pdf"]
    assert sheets["total"] == 0 and sheets["documents"] == []

    bad = await http.get(f"{DOCUMENTS}?space=图库&type=videos", headers=headers)
    assert bad.status_code == 422
