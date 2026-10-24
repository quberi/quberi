# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""tools/list (scope-filtered) and tools/call for the MCP tools.

Each tools/call branch dispatches into the same ``services.mcp.adapters.do_xxx``
function the REST external API calls (see router.py and adapters.py). These
tests therefore focus on the MCP-specific behavior — scope filtering of
tools/list, the JSON-RPC result/error shape, and the content formatting —
not on re-testing retrieval/permission logic that ``test_external_*.py``
already covers for the exact same adapter functions.
"""

from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace

from tests.test_mcp_handshake import (  # noqa: F401
    _mcp_tokens,
    _org,
    _tokens,
    http,
    redis_client,
)

SAVE_NOTE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "save_note", "arguments": {}},
}


def _auth(tokens) -> dict:
    return {"Authorization": f"Bearer {tokens.access_token}"}


def _call(name: str, arguments: dict, request_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def _list_request(request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/list"}


# ---------------------------------------------------------------------------
# tools/list: filtered by scope
# ---------------------------------------------------------------------------


async def test_tools_list_with_full_scope_shows_all_eight(http, db_session, redis_client):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(
        db_session, redis_client, boot, scope="knowledge:read knowledge:write knowledge:upload"
    )
    await db_session.commit()
    resp = await http.post("/", headers=_auth(tokens), json=_list_request())
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert names == {
        "search", "read_around", "get_document_image", "search_images", "list_spaces",
        "list_documents", "save_note", "upload",
    }
    for tool in resp.json()["result"]["tools"]:
        assert set(tool) == {"name", "title", "description", "inputSchema", "annotations"}
        assert tool["inputSchema"]["type"] == "object"


async def test_tools_list_with_read_only_scope_hides_write_tools(
    http, db_session, redis_client
):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot, scope="knowledge:read")
    await db_session.commit()
    resp = await http.post("/", headers=_auth(tokens), json=_list_request())
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert names == {
        "search", "read_around", "get_document_image", "search_images", "list_spaces",
        "list_documents",
    }
    assert "save_note" not in names and "upload" not in names


async def test_tools_list_with_write_only_scope_shows_only_save_note(
    http, db_session, redis_client
):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot, scope="knowledge:write")
    await db_session.commit()
    resp = await http.post("/", headers=_auth(tokens), json=_list_request())
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert names == {"save_note"}


# ---------------------------------------------------------------------------
# tools/call: unknown tool and malformed params are protocol errors
# ---------------------------------------------------------------------------


async def test_calling_an_unknown_tool_is_a_protocol_error_not_a_tool_error(
    http, db_session, redis_client
):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot)
    await db_session.commit()
    resp = await http.post("/", headers=_auth(tokens), json=_call("delete_everything", {}))
    body = resp.json()
    assert "error" in body and "result" not in body
    assert body["error"]["code"] == -32602


async def test_calling_a_tool_without_its_scope_is_a_tool_execution_error(
    http, db_session, redis_client
):
    """insufficient_scope is a tool error (isError: true), not a protocol error (design.md §2.7)."""
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot, scope="knowledge:read")
    await db_session.commit()
    resp = await http.post(
        "/", headers=_auth(tokens), json=_call("save_note", {"title": "t", "text": "b"})
    )
    body = resp.json()
    assert "result" in body  # a JSON-RPC success, carrying a tool-execution error
    result = body["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"].lower()
    assert "permission" in text or "sign in" in text


# ---------------------------------------------------------------------------
# search / search_images / list_spaces / list_documents / save_note / upload
# ---------------------------------------------------------------------------


async def test_search_returns_formatted_text_and_structured_content(
    http, db_session, redis_client, monkeypatch
):
    from quberi_api.services.doc_acl import NO_DOC_ACCESS
    from quberi_api.services.mcp import adapters
    from quberi_api.services.rag.vector_store import RetrievedChunk


    doc, space = uuid.uuid4(), uuid.uuid4()

    async def fake_retrieve(
        session, *, qdrant, organization_id, caller_user_id, query, top_k, **_more
    ):
        chunk = RetrievedChunk(
            document_id=doc, space_id=space, document_name="Handbook.md",
            chunk_index=0, text="Remote work is allowed.", score=0.9, char_start=0, char_end=23,
        )
        return SimpleNamespace(chunks=[chunk], space_ids=[space], doc_access=NO_DOC_ACCESS)

    monkeypatch.setattr(adapters, "retrieve_for_user", fake_retrieve)
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot, scope="knowledge:read")
    await db_session.commit()

    resp = await http.post(
        "/", headers=_auth(tokens), json=_call("search", {"query": "remote work"})
    )
    result = resp.json()["result"]
    assert result["isError"] is False
    assert "Handbook.md" in result["content"][0]["text"]
    assert "Remote work is allowed." in result["content"][0]["text"]
    assert result["structuredContent"]["passages"][0]["document_name"] == "Handbook.md"


async def test_list_spaces_returns_the_personal_space(http, db_session, redis_client):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot, scope="knowledge:read")
    await db_session.commit()
    resp = await http.post("/", headers=_auth(tokens), json=_call("list_spaces", {}))
    result = resp.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["total"] >= 1
    assert any(s["kind"] == "personal" for s in result["structuredContent"]["spaces"])


async def test_list_documents_needs_a_space_name(http, db_session, redis_client):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot, scope="knowledge:read")
    await db_session.commit()
    resp = await http.post("/", headers=_auth(tokens), json=_call("list_documents", {}))
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "space" in result["content"][0]["text"].lower()


async def test_list_documents_an_unknown_space_is_a_tool_error_with_the_adapters_code(
    http, db_session, redis_client
):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot, scope="knowledge:read")
    await db_session.commit()
    resp = await http.post(
        "/", headers=_auth(tokens), json=_call("list_documents", {"space": "no such space"})
    )
    result = resp.json()["result"]
    assert result["isError"] is True


async def test_save_note_lands_in_the_personal_space_by_default(
    http, db_session, redis_client, monkeypatch
):
    from quberi_api.services.documents import core as documents_core

    captured: dict = {}

    async def fake_store(*, storage_key, source_stream, content_type, max_bytes):
        captured["blob"] = source_stream.read()
        return SimpleNamespace(
            storage_key=storage_key, size_bytes=len(captured["blob"]), checksum_sha256=hashlib.sha256(captured["blob"]).hexdigest()
        )

    async def fake_enqueue(document_id):
        return "job-test"

    monkeypatch.setattr(documents_core.local_connector, "store", fake_store)
    monkeypatch.setattr(documents_core, "enqueue_index_document", fake_enqueue)

    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot, scope="knowledge:write")
    await db_session.commit()

    note = {"title": "Meeting", "text": "We decided on plan B."}
    resp = await http.post("/", headers=_auth(tokens), json=_call("save_note", note))
    result = resp.json()["result"]
    assert result["isError"] is False
    assert "Meeting" in result["content"][0]["text"]
    assert "We decided on plan B." in captured["blob"].decode("utf-8")
    assert result["structuredContent"]["space_id"] == str(boot.personal_space_id)


async def test_upload_decodes_base64_and_stores_it(http, db_session, redis_client, monkeypatch):
    import base64

    from quberi_api.services.documents import core as documents_core

    captured: dict = {}

    async def fake_store(*, storage_key, source_stream, content_type, max_bytes):
        captured["blob"] = source_stream.read()
        return SimpleNamespace(
            storage_key=storage_key, size_bytes=len(captured["blob"]), checksum_sha256=hashlib.sha256(captured["blob"]).hexdigest()
        )

    async def fake_enqueue(document_id):
        return "job-test"

    monkeypatch.setattr(documents_core.local_connector, "store", fake_store)
    monkeypatch.setattr(documents_core, "enqueue_index_document", fake_enqueue)

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot, scope="knowledge:upload")
    await db_session.commit()

    resp = await http.post(
        "/",
        headers=_auth(tokens),
        json=_call(
            "upload",
            {"filename": "part.png", "content_base64": base64.b64encode(png).decode()},
        ),
    )
    result = resp.json()["result"]
    assert result["isError"] is False, result
    assert captured["blob"] == png
    assert result["structuredContent"]["mime_type"] == "image/png"


async def test_upload_rejects_invalid_base64_as_a_tool_error(http, db_session, redis_client):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot, scope="knowledge:upload")
    await db_session.commit()
    resp = await http.post(
        "/",
        headers=_auth(tokens),
        json=_call("upload", {"filename": "a.png", "content_base64": "not base64!!"}),
    )
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "base64" in result["content"][0]["text"].lower()


async def test_save_note_with_invalid_input_is_a_tool_error_not_a_crash(
    http, db_session, redis_client
):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot, scope="knowledge:write")
    await db_session.commit()
    resp = await http.post(
        "/", headers=_auth(tokens), json=_call("save_note", {"title": "", "text": "x"})
    )
    result = resp.json()["result"]
    assert result["isError"] is True
