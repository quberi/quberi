"""JSON-RPC 2.0 request/response shapes and error codes for the MCP endpoint.

Only what the server needs to handle ``initialize``, ``notifications/initialized``,
``tools/list`` and ``tools/call`` (design.md §2.1, §1.2). This module has no
knowledge of Quberi's domain — it is pure JSON-RPC/MCP protocol plumbing. The
six tools themselves live in ``schema.py``; the HTTP-level request/notification
branching (the "does this body have an ``id``?" decision) lives in the router
(``api/v1/mcp/router.py``), not here, because that decision determines which
HTTP status code to use and this module does not know about HTTP.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# The only protocol version this server declares support for (design.md §2.1):
# the first MCP revision to make RFC 9728 discovery a MUST, and the version
# Codex's default (``Legacy``) handshake mode negotiates against.
PROTOCOL_VERSION = "2025-06-18"

# JSON-RPC 2.0 standard error codes used here.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class JsonRpcError(BaseModel):
    code: int
    message: str
    data: Optional[Any] = None


class JsonRpcErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"] = "2.0"
    id: Optional[Any] = None
    error: JsonRpcError


class JsonRpcResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"] = "2.0"
    id: Any
    result: dict


def error_response(request_id: Any, code: int, message: str, data: Any = None) -> dict:
    """A JSON-RPC error response body, as a plain dict ready for ``JSONResponse``."""

    return JsonRpcErrorResponse(
        id=request_id, error=JsonRpcError(code=code, message=message, data=data)
    ).model_dump(exclude_none=True)


def result_response(request_id: Any, result: dict) -> dict:
    """A JSON-RPC success response body, as a plain dict ready for ``JSONResponse``."""

    return JsonRpcResultResponse(id=request_id, result=result).model_dump()


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


class InitializeParams(BaseModel):
    model_config = ConfigDict(extra="allow")  # clientInfo/capabilities vary by client

    protocolVersion: str = Field(default="")


SERVER_INFO = {"name": "quberi", "version": "1.0.0"}

# We declare only the ``tools`` capability (design.md §2.1): no resources, no
# prompts, no server-to-client notifications. ``listChanged: False`` because
# the tool list for a given grant never changes mid-connection — it is a pure
# function of the grant's scope, which cannot change without a fresh OAuth flow.
SERVER_CAPABILITIES = {"tools": {"listChanged": False}}


def negotiate_protocol_version(requested: str) -> str:
    """Echo the client's version if we support it, else offer ours (MCP spec's negotiation rule)."""

    return requested if requested == PROTOCOL_VERSION else PROTOCOL_VERSION


def initialize_result(requested_version: str) -> dict:
    return {
        "protocolVersion": negotiate_protocol_version(requested_version),
        "capabilities": SERVER_CAPABILITIES,
        "serverInfo": SERVER_INFO,
    }


# ---------------------------------------------------------------------------
# tools/call result shape
# ---------------------------------------------------------------------------


def text_content(text: str) -> dict:
    return {"type": "text", "text": text}


def tool_result(text: str, structured: Optional[dict] = None, *, is_error: bool = False) -> dict:
    """A ``CallToolResult`` — design.md §2.5: text content plus structuredContent."""

    result: dict = {"content": [text_content(text)], "isError": is_error}
    if structured is not None:
        result["structuredContent"] = structured
    return result
