# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""Tool annotations: what each tool says about itself, so a client can ask only when it matters.

They are hints, not a lock (the permission checks live elsewhere), so what is tested here is that
they are present, complete, and cannot say a write tool only reads.
"""

from __future__ import annotations

from quberi_api.services.mcp import schema
from quberi_api.services.oauth import clients
from tests.test_mcp_handshake import (  # noqa: F401
    CARD_URL,
    _mcp_tokens,
    _org,
    http,
    redis_client,
)

HINTS = {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
READ_TOOLS = {
    "search",
    "read_around",
    "get_document_image",
    "search_images",
    "list_spaces",
    "list_documents",
}
WRITE_TOOLS = {"save_note", "upload"}


def test_every_tool_says_all_four_things_and_has_a_title():
    assert {t.name for t in schema.TOOLS} == READ_TOOLS | WRITE_TOOLS
    for tool in schema.TOOLS:
        assert set(tool.annotations) == HINTS, tool.name
        assert all(isinstance(v, bool) for v in tool.annotations.values()), tool.name
        assert tool.title.strip(), tool.name


def test_the_read_tools_are_read_only_and_the_write_tools_are_not():
    for tool in schema.TOOLS:
        reads = tool.annotations["readOnlyHint"]
        assert reads is (tool.name in READ_TOOLS), tool.name


def test_a_tool_that_needs_a_write_permission_is_never_marked_read_only():
    # The scope is what the server enforces; the hint must not contradict it.
    for tool in schema.TOOLS:
        if tool.required_scope != clients.SCOPE_KNOWLEDGE_READ:
            assert tool.annotations["readOnlyHint"] is False, tool.name
        else:
            assert tool.annotations["readOnlyHint"] is True, tool.name


def test_no_tool_deletes_or_reaches_outside_the_persons_own_knowledge():
    for tool in schema.TOOLS:
        assert tool.annotations["destructiveHint"] is False, tool.name
        assert tool.annotations["openWorldHint"] is False, tool.name


def test_repeating_a_read_changes_nothing_and_a_note_is_added_each_time():
    by_name = {t.name: t.annotations for t in schema.TOOLS}
    assert all(by_name[n]["idempotentHint"] is True for n in READ_TOOLS)
    assert by_name["save_note"]["idempotentHint"] is False  # a new note every call
    assert by_name["upload"]["idempotentHint"] is True  # an identical file is not stored twice


async def test_tools_list_carries_the_annotations_and_hands_out_copies(
    http, db_session, redis_client
):
    boot = await _org(db_session)
    tokens = await _mcp_tokens(db_session, redis_client, boot)
    resp = await http.post(
        "/",
        headers={"Authorization": f"Bearer {tokens.access_token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    listed = {t["name"]: t for t in resp.json()["result"]["tools"]}
    assert set(listed) == READ_TOOLS | WRITE_TOOLS
    for spec in schema.TOOLS:
        assert listed[spec.name]["annotations"] == spec.annotations
        assert listed[spec.name]["title"] == spec.title
    # What a response holds is a copy: nothing done to it can change the table.
    first = schema._listed(schema.TOOLS[0])
    first["annotations"]["readOnlyHint"] = False
    assert schema.TOOLS[0].annotations["readOnlyHint"] is True


async def test_the_server_card_says_the_same_as_tools_list(http):
    card = (await http.get(CARD_URL)).json()
    by_name = {t["name"]: t for t in card["tools"]}
    for spec in schema.TOOLS:
        assert by_name[spec.name]["annotations"] == spec.annotations
        assert by_name[spec.name]["title"] == spec.title
