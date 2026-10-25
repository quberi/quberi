# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""``get_document_image``: a page of a document as an MCP image block, with a caption and a link.

Real: the database, the permission evaluator, the OAuth grant, the PDF drawing (a child process
running pdfium), Pillow, the signed page link and the route that serves it. Stood in: the file
store (a dict) and the Qdrant client (in memory, not used by this tool).
"""

from __future__ import annotations

import base64
import io
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image
from sqlalchemy import select, update

from quberi_api.api import dependencies
from quberi_api.api.v1.external import external_router
from quberi_api.api.v1.mcp.router import mcp_router
from quberi_api.core.config import get_settings
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document
from quberi_api.domain.spaces.models import Space
from quberi_api.services import external_page_links
from quberi_api.services.documents import page_image
from quberi_api.services.mcp import adapters, protocol
from tests.test_document_acl import _member, _rule
from tests.test_external_image_search import ORIGIN, signing_key  # noqa: F401
from tests.test_external_page_and_context import _pdf, _png
from tests.test_external_save import _org, redis_client  # noqa: F401
from tests.test_image_indexing import _doc
from tests.test_image_indexing import _space as _make_space
from tests.test_image_search import _grant
from tests.test_image_search_api import blobs  # noqa: F401
from tests.test_mcp_permission_isolation import _tokens, qdrant  # noqa: F401

NOT_FOUND_TEXT = (
    "No such document or version that you can read. Take the document_id from a search "
    "result, a picture result or a document list."
)


@pytest.fixture
async def http(db_session, redis_client, qdrant):
    app = FastAPI()
    app.state.settings = get_settings()
    app.include_router(mcp_router)
    app.include_router(external_router, prefix="/api/v1")
    infra = SimpleNamespace(redis=redis_client, qdrant=qdrant)

    async def _session():
        yield db_session

    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[dependencies.get_infrastructure] = lambda: infra
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _rpc(arguments: dict, request_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": "get_document_image", "arguments": arguments},
    }


async def _call(http, tokens, arguments: dict) -> dict:
    resp = await http.post(
        "/", headers={"Authorization": f"Bearer {tokens.access_token}"}, json=_rpc(arguments)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


async def _reader(db, redis, boot, user_id=None, scope="knowledge:read"):
    tokens = await _tokens(db, redis, boot, scope=scope, user_id=user_id or boot.user_id)
    await db.commit()
    return tokens


async def _stored(
    db, boot, blobs, *, name="report.pdf", mime="application/pdf", data=None, space=None
):
    space = space or await _make_space(db, boot, "Reports")
    doc = await _doc(
        db,
        boot,
        space,
        name,
        mime=mime,
        state="indexed",
        blobs=blobs,
        data=data if data is not None else _pdf(),
    )
    await db.commit()
    return space, doc


def _decoded(result: dict) -> Image.Image:
    image_blocks = [b for b in result["content"] if b["type"] == "image"]
    assert len(image_blocks) == 1
    return Image.open(io.BytesIO(base64.b64decode(image_blocks[0]["data"], validate=True)))


def _link_in(text: str) -> str:
    """The address on the last line of the caption."""

    last = text.strip().splitlines()[-1]
    assert last.startswith("http"), last
    return last


def _text(result: dict) -> str:
    texts = [b["text"] for b in result["content"] if b["type"] == "text"]
    assert len(texts) == 1
    return texts[0]


# ---------------------------------------------------------------------------
# What a good call returns
# ---------------------------------------------------------------------------


async def test_a_pdf_page_comes_back_as_an_image_block_a_caption_and_a_link(
    http, db_session, redis_client, blobs
):
    boot = await _org(db_session)
    _space, doc = await _stored(db_session, boot, blobs)
    tokens = await _reader(db_session, redis_client, boot)

    result = await _call(http, tokens, {"document_id": str(doc.id), "page": 2})

    assert result["isError"] is False
    # The image first, then the text, and nothing else.
    assert [b["type"] for b in result["content"]] == ["image", "text"]
    block = result["content"][0]
    assert set(block) == {"type", "data", "mimeType"} and block["mimeType"] == "image/jpeg"
    assert not block["data"].startswith("data:")
    image = _decoded(result)
    assert image.format == "JPEG" and image.size[0] == 1024
    assert len(base64.b64decode(block["data"])) <= page_image.DEFAULT_BYTE_CAP

    text = _text(result)
    assert "report.pdf" in text and "page 2" in text and "version 1" in text
    assert f"document_id={doc.id}" in text
    assert "if the image above is not displayed" in text.lower()

    # No structuredContent: a client that prefers it would hand the model only that and drop the
    # image and this caption. The caption carries everything, including the size.
    assert "structuredContent" not in result
    assert "1024x" in text and "KB" in text and "space: Reports" in text


async def test_the_link_in_the_caption_really_opens_the_same_page(
    http, db_session, redis_client, blobs
):
    boot = await _org(db_session)
    _space, doc = await _stored(db_session, boot, blobs)
    tokens = await _reader(db_session, redis_client, boot)

    result = await _call(http, tokens, {"document_id": str(doc.id), "page": 3})
    link = _link_in(_text(result))
    assert link.startswith(f"{ORIGIN}/api/v1/external/page/") and link.endswith(".jpg")
    # No access token in the address: the link is its own short-lived proof.
    assert tokens.access_token not in link

    path = link.removeprefix(ORIGIN)
    opened = await http.get(path)
    assert opened.status_code == 200 and opened.headers["content-type"] == "image/jpeg"
    found = external_page_links.verify(get_settings(), path.rsplit("/", 1)[1][:-4])
    assert (found.document_id, found.page, found.version_number) == (doc.id, 3, 1)


async def test_a_picture_is_shown_at_the_width_asked_for_and_never_enlarged(
    http, db_session, redis_client, blobs
):
    boot = await _org(db_session)
    _space, big = await _stored(
        db_session, boot, blobs, name="part.png", mime="image/png", data=_png((1600, 900))
    )
    _space, small = await _stored(
        db_session, boot, blobs, name="tiny.png", mime="image/png", data=_png((400, 300))
    )
    tokens = await _reader(db_session, redis_client, boot)

    default = _decoded(await _call(http, tokens, {"document_id": str(big.id)}))
    narrow = _decoded(await _call(http, tokens, {"document_id": str(big.id), "max_width": 300}))
    tiny = _decoded(await _call(http, tokens, {"document_id": str(small.id)}))

    assert default.size[0] == 1024  # the picture is 1600 wide: shown at the width asked for
    assert narrow.size[0] <= 300
    assert tiny.size[0] <= 400  # a small picture stays small


async def test_the_width_must_be_within_the_limits(http, db_session, redis_client, blobs):
    boot = await _org(db_session)
    _space, doc = await _stored(db_session, boot, blobs)
    tokens = await _reader(db_session, redis_client, boot)

    for bad in (100, 5000, "wide"):
        result = await _call(http, tokens, {"document_id": str(doc.id), "max_width": bad})
        assert result["isError"] is True
        assert "rejected the input" in _text(result)


# ---------------------------------------------------------------------------
# Honest failures
# ---------------------------------------------------------------------------


async def test_a_page_that_does_not_exist_is_said_plainly(http, db_session, redis_client, blobs):
    boot = await _org(db_session)
    _space, doc = await _stored(db_session, boot, blobs)
    tokens = await _reader(db_session, redis_client, boot)

    result = await _call(http, tokens, {"document_id": str(doc.id), "page": 9})
    assert result["isError"] is True and "no page 9" in _text(result)
    assert [b["type"] for b in result["content"]] == ["text"]


async def test_a_picture_has_only_one_page(http, db_session, redis_client, blobs):
    boot = await _org(db_session)
    _space, doc = await _stored(
        db_session, boot, blobs, name="part.png", mime="image/png", data=_png()
    )
    tokens = await _reader(db_session, redis_client, boot)

    assert (await _call(http, tokens, {"document_id": str(doc.id), "page": 1}))["isError"] is False
    result = await _call(http, tokens, {"document_id": str(doc.id), "page": 2})
    assert result["isError"] is True and "no page 2" in _text(result)


async def test_a_file_with_no_pages_says_what_to_use_instead(http, db_session, redis_client, blobs):
    boot = await _org(db_session)
    _space, doc = await _stored(
        db_session, boot, blobs, name="notes.md", mime="text/markdown", data=b"# notes"
    )
    tokens = await _reader(db_session, redis_client, boot)

    result = await _call(http, tokens, {"document_id": str(doc.id)})
    assert result["isError"] is True
    assert "cannot show this file as an image" in _text(result)
    assert "read_around" in _text(result)


async def test_something_that_only_claims_to_be_a_pdf_is_an_error_not_a_crash(
    http, db_session, redis_client, blobs
):
    boot = await _org(db_session)
    _space, doc = await _stored(db_session, boot, blobs, data=b"%PDF-1.7 not really a pdf")
    tokens = await _reader(db_session, redis_client, boot)

    result = await _call(http, tokens, {"document_id": str(doc.id)})
    assert result["isError"] is True and "could not draw" in _text(result)


async def test_a_version_that_does_not_exist_is_not_found(http, db_session, redis_client, blobs):
    boot = await _org(db_session)
    _space, doc = await _stored(db_session, boot, blobs)
    tokens = await _reader(db_session, redis_client, boot)

    assert (await _call(http, tokens, {"document_id": str(doc.id), "version": 1}))[
        "isError"
    ] is False
    result = await _call(http, tokens, {"document_id": str(doc.id), "version": 4})
    assert result["isError"] is True and _text(result) == NOT_FOUND_TEXT


async def test_without_a_signing_key_the_image_still_comes_and_the_text_says_no_link(
    http, db_session, redis_client, blobs, monkeypatch
):
    boot = await _org(db_session)
    _space, doc = await _stored(db_session, boot, blobs)
    tokens = await _reader(db_session, redis_client, boot)
    monkeypatch.setattr(external_page_links, "links_available", lambda _settings: False)

    result = await _call(http, tokens, {"document_id": str(doc.id)})
    assert result["isError"] is False and _decoded(result).format == "JPEG"
    assert "structuredContent" not in result
    assert "http" not in _text(result)


# ---------------------------------------------------------------------------
# Who may see what: the same answer as read_around gives, for every reason
# ---------------------------------------------------------------------------


async def test_a_document_that_is_not_there_and_one_you_may_not_read_look_the_same(
    http, db_session, redis_client, blobs
):
    boot = await _org(db_session)
    space, secret = await _stored(db_session, boot, blobs, name="secret.pdf")
    stranger = await _member(db_session, boot)  # a member with no access to that space
    tokens = await _reader(db_session, redis_client, boot, user_id=stranger.id)

    unknown = await _call(http, tokens, {"document_id": str(uuid.uuid4())})
    await db_session.commit()  # a request of its own has a session of its own; this one is shared
    hidden = await _call(http, tokens, {"document_id": str(secret.id)})

    assert unknown["isError"] is True and hidden["isError"] is True
    assert _text(unknown) == _text(hidden) == NOT_FOUND_TEXT
    assert hidden["content"] == unknown["content"]
    assert "secret.pdf" not in str(hidden)


async def test_a_member_who_may_read_the_space_gets_the_page(http, db_session, redis_client, blobs):
    boot = await _org(db_session)
    space, doc = await _stored(db_session, boot, blobs)
    reader = await _member(db_session, boot)
    await _grant(db_session, boot, space, SimpleNamespace(id=reader.id), "viewer")
    tokens = await _reader(db_session, redis_client, boot, user_id=reader.id)

    result = await _call(http, tokens, {"document_id": str(doc.id)})
    assert result["isError"] is False and _decoded(result).format == "JPEG"


async def test_a_document_denied_to_one_person_is_not_found_for_them(
    http, db_session, redis_client, blobs
):
    boot = await _org(db_session)
    space, doc = await _stored(db_session, boot, blobs)
    reader = await _member(db_session, boot)
    await _grant(db_session, boot, space, SimpleNamespace(id=reader.id), "viewer")
    tokens = await _reader(db_session, redis_client, boot, user_id=reader.id)
    assert (await _call(http, tokens, {"document_id": str(doc.id)}))["isError"] is False

    await _rule(db_session, boot, space, doc, "deny", "user", reader.id)

    result = await _call(http, tokens, {"document_id": str(doc.id)})
    assert result["isError"] is True and _text(result) == NOT_FOUND_TEXT


async def test_a_document_of_another_organisation_is_not_found(
    http, db_session, redis_client, blobs
):
    mine = await _org(db_session)
    theirs = await _org(db_session)
    _space, foreign = await _stored(db_session, theirs, blobs, name="theirs.pdf")
    tokens = await _reader(db_session, redis_client, mine)

    result = await _call(http, tokens, {"document_id": str(foreign.id)})
    assert result["isError"] is True and _text(result) == NOT_FOUND_TEXT


async def test_an_archived_document_and_an_archived_space_are_not_found(
    http, db_session, redis_client, blobs
):
    boot = await _org(db_session)
    space, gone = await _stored(db_session, boot, blobs, name="gone.pdf")
    other_space, kept = await _stored(db_session, boot, blobs, name="kept.pdf")
    tokens = await _reader(db_session, redis_client, boot)
    await db_session.execute(
        update(Document).where(Document.id == gone.id).values(status="archived")
    )
    await db_session.execute(
        update(Space).where(Space.id == other_space.id).values(status="archived")
    )
    await db_session.commit()

    for doc in (gone, kept):
        result = await _call(http, tokens, {"document_id": str(doc.id)})
        assert result["isError"] is True and _text(result) == NOT_FOUND_TEXT
        await db_session.commit()  # the session is shared between the calls of one test


async def test_a_file_waiting_for_approval_is_not_shown_to_others(
    http, db_session, redis_client, blobs
):
    boot = await _org(db_session)
    space, doc = await _stored(db_session, boot, blobs, name="draft.pdf")
    reader = await _member(db_session, boot)
    await _grant(db_session, boot, space, SimpleNamespace(id=reader.id), "viewer")
    await db_session.execute(
        update(Document)
        .where(Document.id == doc.id)
        .values(review_state="pending", uploaded_by_user_id=boot.user_id)
    )
    await db_session.commit()
    tokens = await _reader(db_session, redis_client, boot, user_id=reader.id)

    result = await _call(http, tokens, {"document_id": str(doc.id)})
    assert result["isError"] is True and _text(result) == NOT_FOUND_TEXT


async def test_the_tool_needs_the_read_permission(http, db_session, redis_client, blobs):
    boot = await _org(db_session)
    _space, doc = await _stored(db_session, boot, blobs)
    tokens = await _reader(db_session, redis_client, boot, scope="knowledge:write")

    listed = await http.post(
        "/",
        headers={"Authorization": f"Bearer {tokens.access_token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert "get_document_image" not in {t["name"] for t in listed.json()["result"]["tools"]}
    result = await _call(http, tokens, {"document_id": str(doc.id)})
    assert result["isError"] is True and [b["type"] for b in result["content"]] == ["text"]


# ---------------------------------------------------------------------------
# Limits and the record of use
# ---------------------------------------------------------------------------


async def test_too_many_calls_in_a_minute_are_refused(
    http, db_session, redis_client, blobs, monkeypatch
):
    boot = await _org(db_session)
    _space, doc = await _stored(db_session, boot, blobs)
    tokens = await _reader(db_session, redis_client, boot)
    monkeypatch.setattr(adapters, "DOCIMAGE_RATE_LIMIT", 2)

    first = await _call(http, tokens, {"document_id": str(doc.id)})
    second = await _call(http, tokens, {"document_id": str(doc.id)})
    third = await _call(http, tokens, {"document_id": str(doc.id)})

    assert first["isError"] is False and second["isError"] is False
    assert third["isError"] is True and "rate limiting" in _text(third)
    assert [b["type"] for b in third["content"]] == ["text"]


async def test_when_the_limiter_cannot_answer_no_image_is_given(
    http, db_session, redis_client, blobs, monkeypatch
):
    from quberi_api.services.oauth import core

    boot = await _org(db_session)
    _space, doc = await _stored(db_session, boot, blobs)
    tokens = await _reader(db_session, redis_client, boot)

    async def _down(*_a, **_kw):
        raise core.OAuthError("temporarily_unavailable", "rate limiter unavailable", status=503)

    monkeypatch.setattr(core, "rate_limit", _down)

    result = await _call(http, tokens, {"document_id": str(doc.id)})
    assert result["isError"] is True
    assert "image" not in {b["type"] for b in result["content"]}


async def test_each_use_is_recorded_without_the_picture(http, db_session, redis_client, blobs):
    boot = await _org(db_session)
    _space, doc = await _stored(db_session, boot, blobs)
    tokens = await _reader(db_session, redis_client, boot)

    result = await _call(http, tokens, {"document_id": str(doc.id), "page": 2})

    events = (
        (
            await db_session.execute(
                select(AuditEvent).where(AuditEvent.action == "knowledge.external_page_image")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    payload = events[0].payload
    assert payload["document_id"] == str(doc.id) and payload["page"] == 2
    assert payload["version"] == 1 and payload["bytes"] > 0 and payload["width"] == 1024
    assert result["content"][0]["data"] not in str(payload)
    assert events[0].actor_id == boot.user_id and events[0].organization_id == boot.organization_id


async def test_a_refused_call_leaves_no_record_of_a_shown_page(
    http, db_session, redis_client, blobs
):
    boot = await _org(db_session)
    _space, doc = await _stored(
        db_session, boot, blobs, name="notes.md", mime="text/markdown", data=b"# n"
    )
    tokens = await _reader(db_session, redis_client, boot)

    await _call(http, tokens, {"document_id": str(doc.id)})
    await db_session.commit()
    await _call(http, tokens, {"document_id": str(uuid.uuid4())})

    events = (
        (
            await db_session.execute(
                select(AuditEvent).where(AuditEvent.action == "knowledge.external_page_image")
            )
        )
        .scalars()
        .all()
    )
    assert events == []


# ---------------------------------------------------------------------------
# The pieces
# ---------------------------------------------------------------------------


def _noise(width: int, height: int) -> bytes:
    import os

    buf = io.BytesIO()
    Image.frombytes("RGB", (width, height), os.urandom(width * height * 3)).save(buf, format="JPEG")
    return buf.getvalue()


def test_fit_jpeg_keeps_a_busy_page_under_the_size_cap_by_giving_up_quality_then_width():
    busy = _noise(1600, 1200)
    assert len(busy) > page_image.DEFAULT_BYTE_CAP

    data, width, height = page_image.fit_jpeg(busy, max_width=1024)

    assert len(data) <= page_image.DEFAULT_BYTE_CAP or width == page_image.MIN_WIDTH
    assert width <= 1024 and height == round(1200 * width / 1600)
    assert Image.open(io.BytesIO(data)).size == (width, height)


def test_fit_jpeg_never_enlarges_and_leaves_a_small_plain_image_alone():
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), "white").save(buf, format="JPEG")

    data, width, height = page_image.fit_jpeg(buf.getvalue(), max_width=1024)

    assert (width, height) == (300, 200) and len(data) <= page_image.DEFAULT_BYTE_CAP


def test_fit_jpeg_returns_its_smallest_try_when_nothing_fits():
    data, width, _height = page_image.fit_jpeg(_noise(1600, 1200), max_width=1600, byte_cap=10)
    assert width == page_image.MIN_WIDTH and len(data) > 10


def test_the_existing_tool_results_still_have_their_old_shape():
    assert protocol.tool_result("hi") == {
        "content": [{"type": "text", "text": "hi"}],
        "isError": False,
    }
    assert protocol.tool_result("hi", {"a": 1}, is_error=True) == {
        "content": [{"type": "text", "text": "hi"}],
        "isError": True,
        "structuredContent": {"a": 1},
    }
    assert protocol.image_content("QUJD", "image/jpeg") == {
        "type": "image",
        "data": "QUJD",
        "mimeType": "image/jpeg",
    }


async def test_the_tool_is_listed_with_a_schema_that_matches_what_it_accepts(
    http, db_session, redis_client
):
    boot = await _org(db_session)
    tokens = await _reader(db_session, redis_client, boot)
    listed = await http.post(
        "/",
        headers={"Authorization": f"Bearer {tokens.access_token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    (tool,) = [t for t in listed.json()["result"]["tools"] if t["name"] == "get_document_image"]
    schema = tool["inputSchema"]
    assert schema["required"] == ["document_id"] and schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"document_id", "page", "version", "max_width"}
    assert schema["properties"]["max_width"]["minimum"] == 256
    assert schema["properties"]["max_width"]["maximum"] == 1600


async def test_the_caption_tells_the_model_to_give_the_link_as_a_link_not_as_a_picture(
    http, db_session, redis_client, blobs
):
    boot = await _org(db_session)
    _space, doc = await _stored(db_session, boot, blobs)
    tokens = await _reader(db_session, redis_client, boot)

    text = _text(await _call(http, tokens, {"document_id": str(doc.id)}))

    assert "ordinary Markdown link" in text
    assert "Do not put it in your answer as an image" in text
    assert "![" in text  # named only to forbid it


async def test_other_tools_still_carry_their_structured_part(http, db_session, redis_client):
    boot = await _org(db_session)
    tokens = await _reader(db_session, redis_client, boot)
    resp = await http.post(
        "/",
        headers={"Authorization": f"Bearer {tokens.access_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_spaces", "arguments": {}},
        },
    )
    assert "structuredContent" in resp.json()["result"]
