"""The MCP tools: their JSON Schemas, scope requirements, and dispatch table.

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

from dataclasses import dataclass, field
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

_READ_AROUND_SCHEMA = {
    "type": "object",
    "properties": {
        "document_id": {
            "type": "string",
            "description": "The document_id of a passage from a search result.",
        },
        "version": {
            "type": "integer",
            "minimum": 1,
            "description": "The passage's version_number from the search result.",
        },
        "char_start": {
            "type": "integer",
            "minimum": 0,
            "description": "The passage's char_start from the search result.",
        },
        "char_end": {
            "type": "integer",
            "minimum": 0,
            "description": "The passage's char_end from the search result.",
        },
        "before": {
            "type": "integer",
            "minimum": 0,
            "maximum": 3000,
            "default": 1500,
            "description": "Characters of text to return before the passage.",
        },
        "after": {
            "type": "integer",
            "minimum": 0,
            "maximum": 3000,
            "default": 1500,
            "description": "Characters of text to return after the passage.",
        },
    },
    "required": ["document_id", "char_start", "char_end"],
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

_GET_DOCUMENT_IMAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "document_id": {
            "type": "string",
            "description": "The document_id from a search result, a picture result or a document list.",  # noqa: E501
        },
        "page": {
            "type": "integer",
            "minimum": 1,
            "description": "The page of a PDF to show. Omit for the first page; a picture has only one.",  # noqa: E501
        },
        "version": {
            "type": "integer",
            "minimum": 1,
            "description": "The version_number from the search result. Omit for the current version.",  # noqa: E501
        },
        "max_width": {
            "type": "integer",
            "minimum": 256,
            "maximum": 1600,
            "default": 1024,
            "description": "The widest the image may be, in pixels.",
        },
    },
    "required": ["document_id"],
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


# What each tool does to the world, as MCP "tool annotations" (spec 2025-03-26 and later).
#
# They are hints a client may use to decide what to ask the person about: a client that
# honours them can run a read-only tool without a prompt and keep asking before a write. They
# are NOT a security boundary (the spec tells clients to treat them as untrusted); what keeps
# data safe is the OAuth scope and the per-document permission checks. They are all written out
# on purpose: left unsaid, the spec's defaults read "may destroy data" and "reaches outside the
# server", which makes a client treat a plain search like a delete.
#
# ``openWorldHint`` is false everywhere: every tool works only on the signed-in person's own
# knowledge base. The read tools do write an audit row, but never touch the person's data.
_READS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
# Adds something new each time; never overwrites or removes anything.
_ADDS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
# Adds, but the same call twice changes nothing more (an identical file is not stored twice).
_ADDS_ONCE = {**_ADDS, "idempotentHint": True}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    required_scope: str
    # A short name for a person to read, and the annotations described above.
    title: str = ""
    annotations: dict = field(default_factory=dict)


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="search",
        description=(
            "Search the knowledge the signed-in person can read: the documents' text, "
            "pictures that might be related, or both. Returns quoted passages as data, each with "
            "where it is in the original file (page, slide, sheet row, paragraph or line) and the "
            "version, and a \"Cite as\" line. When you answer from the results, end each point "
            "with that line, copied as given, and never state a place or version it does not give. "
            "To see the page a passage is on, call get_document_image."
        ),
        input_schema=_SEARCH_SCHEMA,
        required_scope=clients.SCOPE_KNOWLEDGE_READ,
        title="Search knowledge",
        annotations=_READS,
    ),
    ToolSpec(
        name="read_around",
        description=(
            "Read the text on either side of a passage a search returned, from the same file "
            "and version, to see what comes before and after it. Pass the document_id, "
            "version_number, char_start and char_end exactly as the search result gave them. "
            "Not available for text read from a scan or a picture. Returns quoted text as data."
        ),
        input_schema=_READ_AROUND_SCHEMA,
        required_scope=clients.SCOPE_KNOWLEDGE_READ,
        title="Read around a passage",
        annotations=_READS,
    ),
    ToolSpec(
        name="get_document_image",
        description=(
            "Show one page of a document, or a picture, as an image: a PDF page, or a picture "
            "from the knowledge base. Use it when the person asks to see a file, a page, the "
            "original or the source, or when what matters is how something looks (a drawing, a "
            "table, a stamp, a scan whose text may be misread). Pass the document_id (and the "
            "page and version when a search result gave them). The result is the image plus a "
            "caption and a link: if the image is not displayed in this client, give the person "
            "that link. Do not call it for every result, and not for questions the text already "
            "answers; at most a few images per answer. Other kinds of file (Word, slides, "
            "sheets, text) have no page image: use search or read_around for those."
        ),
        input_schema=_GET_DOCUMENT_IMAGE_SCHEMA,
        required_scope=clients.SCOPE_KNOWLEDGE_READ,
        title="Show a page as an image",
        annotations=_READS,
    ),
    ToolSpec(
        name="search_images",
        description=(
            "Find pictures in the readable knowledge base by a description. To show one larger "
            "in the conversation, call get_document_image with its document_id."
        ),
        input_schema=_SEARCH_IMAGES_SCHEMA,
        required_scope=clients.SCOPE_KNOWLEDGE_READ,
        title="Search pictures",
        annotations=_READS,
    ),
    ToolSpec(
        name="list_spaces",
        description="List the knowledge spaces the signed-in person can read.",
        input_schema=_LIST_SPACES_SCHEMA,
        required_scope=clients.SCOPE_KNOWLEDGE_READ,
        title="List spaces",
        annotations=_READS,
    ),
    ToolSpec(
        name="list_documents",
        description="List the documents in one knowledge space (metadata only, no text).",
        input_schema=_LIST_DOCUMENTS_SCHEMA,
        required_scope=clients.SCOPE_KNOWLEDGE_READ,
        title="List documents",
        annotations=_READS,
    ),
    ToolSpec(
        name="save_note",
        description="Save a text note into the readable knowledge base.",
        input_schema=_SAVE_NOTE_SCHEMA,
        required_scope=clients.SCOPE_KNOWLEDGE_WRITE,
        title="Save a note",
        annotations=_ADDS,
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
        title="Upload a file",
        annotations=_ADDS_ONCE,
    ),
)

_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}


def get_tool(name: str) -> ToolSpec | None:
    return _BY_NAME.get(name)


def tools_list_for(ext: core.ExternalCaller) -> list[dict[str, Any]]:
    """The ``tools`` array of a ``tools/list`` response, filtered by the grant's scope."""

    return [_listed(t) for t in TOOLS if ext.has_scope(t.required_scope)]


def _listed(t: ToolSpec, *, description: str | None = None) -> dict[str, Any]:
    """One tool as ``tools/list`` and the server card word it."""

    return {
        "name": t.name,
        "title": t.title,
        "description": t.description if description is None else description,
        "inputSchema": t.input_schema,
        "annotations": dict(t.annotations),
    }


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
            _listed(
                t,
                description=f"{t.description} {_SCOPE_NOTE.get(t.required_scope, '')}".strip(),
            )
            for t in TOOLS
        ],
        "resources": [],
        "prompts": [],
    }
