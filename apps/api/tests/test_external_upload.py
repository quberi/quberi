# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""Uploading a file through the external-tools API.

``POST /api/v1/external/upload`` needs its own scope, ``knowledge:upload``. A
file is a larger power than a text note, so no existing grant gains it by
accident, and it implies neither reading nor saving notes.

What these tests pin:
* only a grant that holds ``knowledge:upload`` can upload (read, write and
  read+write are refused, with nothing stored);
* upload alone does not let a tool search or save notes;
* the file is checked by its content (see ``test_external_upload_check.py`` for
  the rules); a refusal stores nothing and enqueues nothing;
* it runs as the grant's own person: a space they cannot edit is a clean 403, an
  unknown space name a 404;
* size is capped, in pieces, without trusting the declared length;
* it is rate limited per grant and written to the audit log;
* a revoked grant, or an organisation that turned outside tools off, stops it.

MinIO and the index queue are faked (as in ``test_external_save.py``).
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.identity.models import Organization
from quberi_api.domain.oauth.models import OAuthGrant
from quberi_api.services import documents as documents_service
from quberi_api.services.mcp import adapters
from tests.test_external_save import (  # noqa: F401
    _auth,
    _no_infra,
    _org,
    _tokens,
    http,
    redis_client,
)

UPLOAD = "/api/v1/external/upload"
SAVE = "/api/v1/external/save"
SEARCH = "/api/v1/external/search"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"x" * 64
ALL = "knowledge:read knowledge:write knowledge:upload"


def _files(data: bytes = PNG, name: str = "part.png", ctype: str = "image/png"):
    return {"file": (name, data, ctype)}


async def _grant(db, redis, boot, scope):
    tokens = await _tokens(db, redis, boot, scope=scope)
    await db.commit()
    return tokens


# --------------------------------------------------------------------------
# The scope gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scope",
    ["knowledge:read", "knowledge:write", "knowledge:read knowledge:write"],
)
async def test_a_grant_without_the_upload_scope_cannot_upload(
    http, db_session, redis_client, _no_infra, scope
):
    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, scope)
    resp = await http.post(UPLOAD, headers=_auth(tokens), files=_files())
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "insufficient_scope"
    assert "blob" not in _no_infra  # nothing was stored
    assert "enqueued" not in _no_infra  # nothing was queued for indexing


async def test_the_scope_is_checked_before_the_body_is_read(
    http, db_session, redis_client, _no_infra, monkeypatch
):
    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, "knowledge:read")

    async def boom(*a, **k):
        raise AssertionError("the body must not be read for a grant without the scope")

    monkeypatch.setattr(adapters, "read_capped", boom)
    resp = await http.post(UPLOAD, headers=_auth(tokens), files=_files())
    assert resp.status_code == 403


async def test_upload_alone_cannot_search_or_save_notes(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, "knowledge:upload")
    assert (await http.post(SEARCH, headers=_auth(tokens), json={"query": "x"})).status_code == 403
    saved = await http.post(SAVE, headers=_auth(tokens), json={"title": "t", "text": "b"})
    assert saved.status_code == 403
    assert "blob" not in _no_infra


async def test_an_upload_only_grant_can_upload(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, "knowledge:upload")
    resp = await http.post(UPLOAD, headers=_auth(tokens), files=_files())
    assert resp.status_code == 201, resp.text


async def test_no_token_is_refused(http, _no_infra):
    resp = await http.post(UPLOAD, files=_files())
    assert resp.status_code == 401
    assert "blob" not in _no_infra


# --------------------------------------------------------------------------
# Happy path and where the file goes
# --------------------------------------------------------------------------


async def test_a_file_lands_in_the_personal_space_by_default(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, ALL)
    files = _files(PDF, "nist.pdf", "application/pdf")
    resp = await http.post(UPLOAD, headers=_auth(tokens), files=files)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["space_id"] == str(boot.personal_space_id)
    assert body["name"] == "nist.pdf"
    assert body["mime_type"] == "application/pdf"
    assert body["size_bytes"] == len(PDF)
    assert _no_infra["blob"] == PDF  # the exact bytes reached storage
    assert _no_infra["enqueued"]  # and indexing was queued


async def test_the_stored_type_is_the_detected_type_not_the_declared_one(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, ALL)
    resp = await http.post(
        UPLOAD, headers=_auth(tokens), files=_files(PNG, "photo.png", "application/octet-stream")
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["mime_type"] == "image/png"


async def test_the_stored_name_cannot_pretend_to_be_html(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, ALL)
    files = _files(PNG, "page.html", "image/png")
    resp = await http.post(UPLOAD, headers=_auth(tokens), files=files)
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "page.html.png"


async def test_the_document_records_where_it_came_from(
    http, db_session, redis_client, _no_infra
):
    from quberi_api.domain.documents.models import Document

    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, ALL)
    resp = await http.post(UPLOAD, headers=_auth(tokens), files=_files())
    doc = await db_session.get(Document, resp.json()["document_id"])
    assert doc.attributes["origin"] == "external_upload"
    assert doc.attributes["source_label"] == "DeepSeek Harness"
    assert doc.uploaded_by_user_id == boot.user_id


# --------------------------------------------------------------------------
# Refusals store nothing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data,name,ctype,expected",
    [
        (b"PK\x03\x04" + b"\x00" * 40, "a.zip", "application/zip", 400),
        (b"\x1f\x8b\x08\x00" + b"\x00" * 20, "a.tar.gz", "application/gzip", 415),
        (b"<html></html>", "a.html", "text/html", 415),
        (b"MZ\x90\x00" + b"\x00" * 40, "a.exe", "application/octet-stream", 415),
        (PDF, "a.png", "image/png", 400),  # content is a PDF, label says PNG
        (b"", "empty.txt", "text/plain", 400),
    ],
)
async def test_refused_files_store_nothing(
    http, db_session, redis_client, _no_infra, data, name, ctype, expected
):
    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, ALL)
    resp = await http.post(UPLOAD, headers=_auth(tokens), files=_files(data, name, ctype))
    assert resp.status_code == expected, resp.text
    assert "blob" not in _no_infra
    assert "enqueued" not in _no_infra


# --------------------------------------------------------------------------
# Size
# --------------------------------------------------------------------------


async def test_a_file_over_the_limit_is_refused_without_being_stored(
    http, db_session, redis_client, _no_infra, monkeypatch
):
    # Shrink the limit so the test stays fast; the code path is the same.
    monkeypatch.setattr(documents_service, "MAX_UPLOAD_BYTES", 2 * 1024 * 1024)
    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, ALL)
    big = PNG + b"\x00" * (3 * 1024 * 1024)
    resp = await http.post(UPLOAD, headers=_auth(tokens), files=_files(big))
    assert resp.status_code == 413
    assert resp.json()["detail"]["code"] == "file_too_large"
    assert "blob" not in _no_infra


async def test_the_limit_is_enforced_while_reading_not_only_from_the_header(
    http, db_session, redis_client, _no_infra, monkeypatch
):
    """A request whose declared length is honest-looking but whose body is not."""
    monkeypatch.setattr(documents_service, "MAX_UPLOAD_BYTES", 1024 * 1024)
    seen = {"read": 0}
    real = adapters.read_capped

    async def counting(upload, limit):
        original = upload.read

        async def tracked(n=-1):
            piece = await original(n)
            seen["read"] += len(piece)
            return piece

        upload.read = tracked
        return await real(upload, limit)

    monkeypatch.setattr(adapters, "read_capped", counting)
    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, ALL)
    big = PNG + b"\x00" * (4 * 1024 * 1024)
    resp = await http.post(UPLOAD, headers=_auth(tokens), files=_files(big))
    assert resp.status_code == 413
    # It stopped reading shortly after the limit instead of swallowing it all.
    assert seen["read"] <= 1024 * 1024 + 2 * 1024 * 1024
    assert "blob" not in _no_infra


# --------------------------------------------------------------------------
# It acts as the person
# --------------------------------------------------------------------------


async def test_an_unknown_space_name_is_404_and_stores_nothing(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, ALL)
    resp = await http.post(
        UPLOAD, headers=_auth(tokens), files=_files(), data={"space": "No such space"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "space_not_found"
    assert "blob" not in _no_infra


# --------------------------------------------------------------------------
# Rate limit, audit, and stopping
# --------------------------------------------------------------------------


async def test_upload_is_rate_limited_per_grant(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    a = await _grant(db_session, redis_client, boot, ALL)
    b = await _grant(db_session, redis_client, boot, ALL)
    limit = 10  # _UPLOAD_RATE_LIMIT in api/v1/external.py
    for i in range(limit):
        resp = await http.post(UPLOAD, headers=_auth(a), files=_files(PNG, f"p{i}.png"))
        assert resp.status_code == 201, (i, resp.text)
    over = await http.post(UPLOAD, headers=_auth(a), files=_files())
    assert over.status_code == 429
    assert over.headers.get("retry-after") == "60"
    # Another grant is not affected.
    assert (await http.post(UPLOAD, headers=_auth(b), files=_files())).status_code == 201


async def test_an_upload_is_audited_without_its_content(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, ALL)
    files = _files(PDF, "secret-plan.pdf", "application/pdf")
    resp = await http.post(UPLOAD, headers=_auth(tokens), files=files)
    assert resp.status_code == 201
    events = (
        await db_session.scalars(
            select(AuditEvent).where(AuditEvent.action == "knowledge.external_upload")
        )
    ).all()
    assert len(events) == 1
    payload = events[0].payload
    assert payload["client_name"] == "DeepSeek Harness"
    assert payload["document_id"] == resp.json()["document_id"]
    assert payload["mime"] == "application/pdf"
    assert payload["size_bytes"] == len(PDF)
    # The file's bytes and its text never go into the audit log.
    assert "x" * 16 not in str(payload)
    assert "blob" not in str(payload)


async def test_a_refused_upload_writes_no_audit_event(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, ALL)
    files = _files(b"<html/>", "a.html", "text/html")
    await http.post(UPLOAD, headers=_auth(tokens), files=files)
    events = (
        await db_session.scalars(
            select(AuditEvent).where(AuditEvent.action == "knowledge.external_upload")
        )
    ).all()
    assert events == []


async def test_a_revoked_grant_cannot_upload(http, db_session, redis_client, _no_infra):
    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, ALL)
    grant = (await db_session.scalars(select(OAuthGrant))).all()[-1]
    grant.revoked_at = datetime.now(timezone.utc)
    grant.revoke_reason = "revoked_by_user"
    await db_session.commit()
    resp = await http.post(UPLOAD, headers=_auth(tokens), files=_files())
    assert resp.status_code == 401
    assert "blob" not in _no_infra


async def test_turning_the_organisation_switch_off_stops_uploads_at_once(
    http, db_session, redis_client, _no_infra
):
    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, ALL)
    assert (await http.post(UPLOAD, headers=_auth(tokens), files=_files())).status_code == 201
    org = await db_session.get(Organization, boot.organization_id)
    org.allow_external_tools = False
    await db_session.commit()
    before = _no_infra.get("blob")
    files = _files(PDF, "late.pdf", "application/pdf")
    resp = await http.post(UPLOAD, headers=_auth(tokens), files=files)
    assert resp.status_code in (401, 403)
    assert _no_infra.get("blob") == before


# --------------------------------------------------------------------------
# Office documents are accepted only when real
# --------------------------------------------------------------------------


async def test_a_real_looking_docx_is_accepted_and_a_plain_zip_renamed_is_not(
    http, db_session, redis_client, _no_infra
):
    import zipfile

    def make(entries):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for k, v in entries.items():
                zf.writestr(k, v)
        return buf.getvalue()

    docx = make({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<x/>"})
    renamed_zip = make({"hello.txt": b"hi"})
    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, ALL)
    docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ok = await http.post(UPLOAD, headers=_auth(tokens), files=_files(docx, "a.docx", docx_mime))
    assert ok.status_code == 201, ok.text
    renamed = _files(renamed_zip, "a.docx", docx_mime)
    bad = await http.post(UPLOAD, headers=_auth(tokens), files=renamed)
    assert bad.status_code == 415


# --------------------------------------------------------------------------
# Gaps found by mutation checks: each of these failed to catch a real change
# --------------------------------------------------------------------------


async def test_a_space_the_person_can_only_view_is_403_and_stores_nothing(
    http, db_session, redis_client, _no_infra
):
    import uuid

    from quberi_api.domain.identity.models import User
    from quberi_api.services import members as members_service
    from quberi_api.services import space_bindings as space_bindings_service
    from quberi_api.services import spaces as spaces_service

    boot = await _org(db_session)
    view = await spaces_service.create_collaborative_space(
        db_session,
        organization_id=boot.organization_id,
        creator_user_id=boot.user_id,
        name="只读区",
        slug=f"ro-{uuid.uuid4().hex[:8]}",
        owner="organization",
    )
    await db_session.commit()
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
        space_id=view.space.id,
        organization_id=boot.organization_id,
        actor_user_id=boot.user_id,
        subject_type="user",
        subject_id=member.id,
        role_code="viewer",
    )
    await db_session.commit()
    tokens = await _tokens(db_session, redis_client, boot, scope=ALL, user_id=member.id)
    await db_session.commit()

    resp = await http.post(
        UPLOAD, headers=_auth(tokens), files=_files(), data={"space": "只读区"}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "forbidden"
    assert "blob" not in _no_infra
    assert "enqueued" not in _no_infra


async def test_the_database_row_holds_the_detected_type_not_the_declared_one(
    http, db_session, redis_client, _no_infra
):
    """The response is built by us; the document row is what the app later serves."""
    from quberi_api.domain.documents.models import Document

    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, ALL)
    # The client calls a PNG "text/plain"... that is refused as a mismatch; so
    # use the one lie that is let through (the generic type) and check the row.
    resp = await http.post(
        UPLOAD, headers=_auth(tokens), files=_files(PNG, "photo.png", "application/octet-stream")
    )
    assert resp.status_code == 201, resp.text
    doc = await db_session.get(Document, resp.json()["document_id"])
    assert doc.mime_type == "image/png"
    assert doc.name == "photo.png"
    # What reached object storage was labelled with the detected type as well.
    assert _no_infra["content_type"] == "image/png"


async def test_our_own_size_cap_works_even_if_storage_had_no_limit(
    http, db_session, redis_client, _no_infra, monkeypatch
):
    """The storage layer also caps size; this proves the cap in front of it is real."""
    from quberi_api.services.documents import core as documents_core
    from tests.test_external_save import _StoreResult

    async def unlimited_store(*, storage_key, source_stream, content_type, max_bytes):
        blob = source_stream.read()  # ignores max_bytes entirely
        _no_infra["blob"] = blob
        return _StoreResult(storage_key=storage_key, size_bytes=len(blob), checksum_sha256="0" * 64)

    monkeypatch.setattr(documents_core.local_connector, "store", unlimited_store)
    monkeypatch.setattr(documents_service, "MAX_UPLOAD_BYTES", 1024 * 1024)
    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, ALL)
    big = PNG + b"\x00" * (3 * 1024 * 1024)
    resp = await http.post(UPLOAD, headers=_auth(tokens), files=_files(big))
    assert resp.status_code == 413
    assert "blob" not in _no_infra


async def test_the_capped_read_stops_a_body_whose_declared_length_lies(
    http, db_session, redis_client, _no_infra, monkeypatch
):
    """The header check is only a shortcut. A client can declare a small length and
    send a large body; only the read in pieces can stop that, so test it alone."""
    from quberi_api.services.documents import core as documents_core
    from tests.test_external_save import _StoreResult

    # Storage with no limit, so nothing downstream can mask a missing cap here.
    async def unlimited_store(*, storage_key, source_stream, content_type, max_bytes):
        blob = source_stream.read()
        _no_infra["blob"] = blob
        return _StoreResult(storage_key=storage_key, size_bytes=len(blob), checksum_sha256="0" * 64)

    monkeypatch.setattr(documents_core.local_connector, "store", unlimited_store)
    monkeypatch.setattr(documents_service, "MAX_UPLOAD_BYTES", 1024 * 1024)

    boot = await _org(db_session)
    tokens = await _grant(db_session, redis_client, boot, ALL)
    big = PNG + b"\x00" * (3 * 1024 * 1024)
    request = http.build_request(
        "POST", UPLOAD, headers=_auth(tokens), files=_files(big)
    )
    real_length = int(request.headers["content-length"])
    assert real_length > 3 * 1024 * 1024  # the body really is large...
    request.headers["content-length"] = "1000"  # ...but the header says otherwise
    resp = await http.send(request)
    assert resp.status_code == 413
    assert resp.json()["detail"]["code"] == "file_too_large"
    assert "blob" not in _no_infra
