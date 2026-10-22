"""The six MCP tools: their JSON Schemas, scope requirements, and dispatch table.

Design.md §2.5. Each tool maps one-to-one to a ``do_xxx`` function in
``adapters.py`` and one Pydantic request model in ``api/v1/external_schemas.py``
(the exact same model the REST endpoint validates against — see that module's
docstring and the brief's "do not reimplement retrieval/permission logic" rule).

``tools/list`` does not return a fixed six-tool list: it filters by the
caller's granted scope (design.md §2.5 "tools/list 按 scope 过滤"), reusing the
same ``ExternalCaller.has_scope`` check ``tools/call`` already relies on. A
grant with only ``knowledge:read`` sees four tools, never ``save_note`` or
``upload`` — showing a tool the model can never call is worse than not
showing it (it only produces repeated ``insufficient_scope`` round-trips).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from quberi_api.services.oauth import clients, core

# ---------------------------------------------------------------------------
# JSON Schemas (draft-07-ish subset; what MCP's tools/list expects for
# ``inputSchema``). Field constraints mirror the Pydantic models in
# external_schemas.py exactly, so a client that honours the schema never
# sends input the adapter would reject anyway.
# ---------------------------------------------------------------------------

_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 2000},
        "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        "space": {
            "type": "string",
            "maxLength": 200,
            "description": "Name of one space to look in; omit to look in every space you can read.",  # noqa: E501
        },
        "kind": {
            "type": "string",
            "enum": ["text", "image", "both"],
            "default": "both",
            "description": "What to look in: the documents' text, pictures, or both (default).",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

_SEARCH_IMAGES_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 500},
        "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 6},
        "space": {"type": "string", "maxLength": 200},
    },
    "required": ["query"],
    "additionalProperties": False,
}

_LIST_SPACES_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}

_LIST_DOCUMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "space": {"type": "string", "minLength": 1, "maxLength": 200},
        "cursor": {
            "type": "string",
            "maxLength": 500,
            "description": "Opaque position from a previous call's next_cursor, to get the next page.",  # noqa: E501
        },
        "type": {
            "type": "string",
            "enum": ["image", "pdf", "word", "spreadsheet", "presentation", "text", "other"],
        },
    },
    "required": ["space"],
    "additionalProperties": False,
}

_SAVE_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "text": {"type": "string", "minLength": 1},
        "space": {
            "type": "string",
            "maxLength": 200,
            "description": "Named in words, the way the person does. Omit to use the personal space.",  # noqa: E501
        },
        "source_label": {"type": "string", "maxLength": 200},
        "source_url": {"type": "string", "maxLength": 2000},
    },
    "required": ["title", "text"],
    "additionalProperties": False,
}

_UPLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "filename": {"type": "string", "minLength": 1},
        "content_base64": {
            "type": "string",
            "description": "The file's content, base64-encoded.",
        },
        "space": {"type": "string", "maxLength": 200},
    },
    "required": ["filename", "content_base64"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    required_scope: str


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="search",
        description=(
            "Search the knowledge the signed-in person can read: the documents' text, "
            "pictures that might be related, or both. Returns quoted passages as data, with the "
            "page of the original file when it is known."
        ),
        input_schema=_SEARCH_SCHEMA,
        required_scope=clients.SCOPE_KNOWLEDGE_READ,
    ),
    ToolSpec(
        name="search_images",
        description="Find pictures in the readable knowledge base by a description.",
        input_schema=_SEARCH_IMAGES_SCHEMA,
        required_scope=clients.SCOPE_KNOWLEDGE_READ,
    ),
    ToolSpec(
        name="list_spaces",
        description="List the knowledge spaces the signed-in person can read.",
        input_schema=_LIST_SPACES_SCHEMA,
        required_scope=clients.SCOPE_KNOWLEDGE_READ,
    ),
    ToolSpec(
        name="list_documents",
        description="List the documents in one knowledge space (metadata only, no text).",
        input_schema=_LIST_DOCUMENTS_SCHEMA,
        required_scope=clients.SCOPE_KNOWLEDGE_READ,
    ),
    ToolSpec(
        name="save_note",
        description="Save a text note into the readable knowledge base.",
        input_schema=_SAVE_NOTE_SCHEMA,
        required_scope=clients.SCOPE_KNOWLEDGE_WRITE,
    ),
    ToolSpec(
        name="upload",
        description=(
            "Upload a file (already base64-encoded) into the readable knowledge base. "
            "A file identical to one already in the space is not stored twice: the answer "
            "says it is a duplicate and names the existing file."
        ),
        input_schema=_UPLOAD_SCHEMA,
        required_scope=clients.SCOPE_KNOWLEDGE_UPLOAD,
    ),
)

_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}


def get_tool(name: str) -> ToolSpec | None:
    return _BY_NAME.get(name)


def tools_list_for(ext: core.ExternalCaller) -> list[dict[str, Any]]:
    """The ``tools`` array of a ``tools/list`` response, filtered by the grant's scope."""

    return [
        {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
        for t in TOOLS
        if ext.has_scope(t.required_scope)
    ]


_SCOPE_NOTE = {
    clients.SCOPE_KNOWLEDGE_READ: "Needs the knowledge:read permission.",
    clients.SCOPE_KNOWLEDGE_WRITE: "Needs the knowledge:write permission.",
    clients.SCOPE_KNOWLEDGE_UPLOAD: "Needs the knowledge:upload permission.",
}


def server_card(server_info: dict[str, str]) -> dict[str, Any]:
    """A static description of this server for directories that cannot sign in to scan it.

    It lists every tool the server offers, exactly as ``tools/list`` words them, plus the
    permission each needs (a signed-in grant only sees the tools its permissions allow). It is
    built from the same ``TOOLS`` table, so it cannot drift from what the server really does.
    Nothing here depends on a person, an organisation or any stored data.
    """

    return {
        "serverInfo": {**server_info, "title": "Quberi"},
        "authentication": {"required": True, "schemes": ["oauth2"]},
        "tools": [
            {
                "name": t.name,
                "description": f"{t.description} {_SCOPE_NOTE.get(t.required_scope, '')}".strip(),
                "inputSchema": t.input_schema,
            }
            for t in TOOLS
        ],
        "resources": [],
        "prompts": [],
    }
