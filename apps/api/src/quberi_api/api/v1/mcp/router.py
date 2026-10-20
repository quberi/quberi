"""The MCP Streamable HTTP endpoint: ``POST/GET /`` (design.md §2.1, §2.11).

Mounted at the application root (not under ``/api/v1``) because the
production deployment puts this behind the independent ``mcp.quberi.com``
domain, which Caddy reverse-proxies straight into this container with no
path prefix (design.md §2.2 — the discovery metadata's ``resource`` field is
the bare domain, so the endpoint itself has to be the domain's root path for
RFC 9728's "resource equals the endpoint's own canonical URI" rule to hold).

This module is a thin JSON-RPC/HTTP adapter, exactly like the REST routes in
``api/v1/external.py``: all six tools dispatch into the same
``services.mcp.adapters.do_xxx`` functions the REST endpoints call, so there
is exactly one implementation of retrieval/permission/rate-limit/audit logic
for both protocols (the brief's central rule; see adapters.py's docstring).
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session, get_infrastructure
from quberi_api.api.v1.external_schemas import (
    ImageSearchRequest,
    SaveRequest,
    SearchRequest,
)
from quberi_api.infrastructure.registry import Infrastructure
from quberi_api.services.mcp import adapters, formatting, protocol, schema
from quberi_api.services.oauth import core

router = APIRouter(tags=["mcp"])

_bearer = HTTPBearer(auto_error=False)

# Where a 401's WWW-Authenticate header points a client to discover this
# server's OAuth metadata. Not derived from ``settings.public_base_url``
# (design.md §2.2): ``mcp.quberi.com`` and ``api.quberi.com`` are two
# different domains, so there is nothing to derive it from. The discovery
# document at this address is served by Caddy, not by this API process (see
# the module docstring and design.md §2.2) — this constant only needs to
# match what Caddy already serves in production.
_RESOURCE_METADATA_URL = "https://mcp.quberi.com/.well-known/oauth-protected-resource"

# The only client_id this endpoint accepts (design.md §2.2's minimal audience
# check): a token that is not for this client_id was not issued for this
# resource. See services/oauth/clients.py's CLIENTS registration and
# schema.py's module docstring for why this string must never be reused for
# a differently-scoped MCP registration.
_MCP_CLIENT_ID = "quberi-mcp"


def _www_authenticate(error: str = "invalid_token") -> dict:
    return {
        "WWW-Authenticate": f'Bearer error="{error}", resource_metadata="{_RESOURCE_METADATA_URL}"'
    }


async def require_mcp_caller(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    session: AsyncSession = Depends(get_db_session),
) -> core.ExternalCaller:
    """Resolve the bearer token, same as the REST path, plus the MCP audience check.

    Design.md §2.2: this reuses ``authenticate_access_token`` verbatim (the
    exact same validity/revocation/organisation-switch checks the REST
    external API gets), then additionally requires ``client_id ==
    "quberi-mcp"`` — a minimal, non-RFC-8707 stand-in for audience
    restriction (see the design doc for the known gap this leaves: a
    quberi-mcp token is not rejected by ``/api/v1/external/*``).
    """

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing access token",
            headers=_www_authenticate(),
        )
    try:
        caller = await core.authenticate_access_token(session, credentials.credentials)
    except core.ExternalAuthError as exc:
        headers = _www_authenticate(exc.code) if exc.status == 401 else None
        raise HTTPException(
            status_code=exc.status,
            detail={"code": exc.code, "message": exc.detail},
            headers=headers,
        ) from exc
    if caller.client_id != _MCP_CLIENT_ID:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "invalid_token",
                "message": "this token was not issued for this server",
            },
            headers=_www_authenticate(),
        )
    return caller


@router.get("/")
async def mcp_get() -> Response:
    """No server-to-client push is offered: a real 405, not a fake SSE stream (design.md §2.1)."""

    return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED)


def _rpc_error(request_id: Any, code: int, message: str) -> Response:
    return _json(protocol.error_response(request_id, code, message), status_code=200)


def _json(body: dict, *, status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(body), media_type="application/json", status_code=status_code
    )


@router.post("/")
async def mcp_post(
    request: Request,
    ext: core.ExternalCaller = Depends(require_mcp_caller),
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> Response:
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        return _rpc_error(None, protocol.PARSE_ERROR, "invalid JSON")
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        bad_id = body.get("id") if isinstance(body, dict) else None
        return _rpc_error(bad_id, protocol.INVALID_REQUEST, "not a JSON-RPC 2.0 request")

    # The id-presence branch happens before any method-name dispatch (design.md
    # §2.1/§2.11): a body with no "id" is a notification or a response, which
    # the Streamable HTTP spec requires answering with 202 and an empty body —
    # there is no way to build a JSON-RPC response that echoes a missing id.
    if "id" not in body:
        # notifications/initialized (and any other notification) needs no
        # action: this server is stateless (design.md §2.9), so there is no
        # per-connection "initialized" flag to set.
        return Response(status_code=status.HTTP_202_ACCEPTED)

    request_id = body["id"]
    method = body.get("method")
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return _rpc_error(request_id, protocol.INVALID_PARAMS, "params must be an object")

    if method == "initialize":
        return _json(protocol.result_response(request_id, _initialize(params)))
    if method == "tools/list":
        return _json(
            protocol.result_response(request_id, {"tools": schema.tools_list_for(ext)})
        )
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments")
        name_ok = isinstance(name, str)
        args_ok = arguments is None or isinstance(arguments, dict)
        if not name_ok or not args_ok:
            return _rpc_error(
                request_id,
                protocol.INVALID_PARAMS,
                "tools/call needs a string name and an object arguments",
            )
        if schema.get_tool(name) is None:
            # Design.md §2.7: an unknown tool name is a protocol-level problem
            # (there is no tool to execute), so it is a JSON-RPC error — not a
            # tool-execution error (isError: true) and not an HTTP-level error.
            return _rpc_error(request_id, protocol.INVALID_PARAMS, f"unknown tool: {name!r}")
        return _json(
            protocol.result_response(
                request_id,
                await _tools_call(name, arguments or {}, request, ext, session, infra),
            )
        )
    return _rpc_error(request_id, protocol.METHOD_NOT_FOUND, f"unknown method: {method!r}")


def _initialize(params: dict) -> dict:
    try:
        parsed = protocol.InitializeParams.model_validate(params)
    except ValidationError:
        parsed = protocol.InitializeParams()
    return protocol.initialize_result(parsed.protocolVersion)


# ---------------------------------------------------------------------------
# tools/call dispatch — one branch per tool, each ending in the same
# adapters.do_xxx / formatting.format_xxx pair the REST route for that
# capability uses (see each adapter function and schema.py's ToolSpec table).
# ---------------------------------------------------------------------------


async def _tools_call(
    name: str,
    arguments: dict,
    request: Request,
    ext: core.ExternalCaller,
    session: AsyncSession,
    infra: Infrastructure,
) -> dict:
    """Dispatch a known tool name (``mcp_post`` already validated it exists)."""

    try:
        if name == "search":
            payload = SearchRequest.model_validate(arguments)
            result = await adapters.do_search(payload, request, ext, session, infra)
            return protocol.tool_result(
                formatting.format_search_result(payload.query, result),
                result.model_dump(mode="json"),
            )
        if name == "search_images":
            payload = ImageSearchRequest.model_validate(arguments)
            result = await adapters.do_search_images(payload, request, ext, session, infra)
            return protocol.tool_result(
                formatting.format_image_search_result(payload.query, result),
                result.model_dump(mode="json"),
            )
        if name == "list_spaces":
            result = await adapters.do_list_spaces(ext, session, infra)
            return protocol.tool_result(
                formatting.format_spaces_result(result), result.model_dump(mode="json")
            )
        if name == "list_documents":
            space = arguments.get("space")
            if not isinstance(space, str) or not space.strip():
                return protocol.tool_result(
                    "Say which space to list. Use list_spaces to see the names.", is_error=True
                )
            cursor = arguments.get("cursor")
            doc_type = arguments.get("type")
            result = await adapters.do_list_documents(
                ext,
                session,
                infra,
                space=space,
                cursor=cursor if isinstance(cursor, str) else None,
                type=doc_type if isinstance(doc_type, str) else None,
            )
            return protocol.tool_result(
                formatting.format_documents_result(result), result.model_dump(mode="json")
            )
        if name == "save_note":
            payload = SaveRequest.model_validate(arguments)
            result = await adapters.do_save_note(payload, ext, session, infra)
            return protocol.tool_result(
                formatting.format_save_result(result), result.model_dump(mode="json")
            )
        if name == "upload":
            return await _call_upload(arguments, ext, session)
    except ValidationError as exc:
        return protocol.tool_result(
            f"Quberi rejected the input: {_validation_message(exc)}", is_error=True
        )
    except adapters.AdapterHTTPError as exc:
        return protocol.tool_result(formatting.format_error(exc), is_error=True)

    # schema.get_tool already validated the name against the same TOOLS tuple
    # these branches cover, so this is unreachable — kept as a defensive 500
    # rather than a silent None return if the two ever drift apart.
    raise HTTPException(status_code=500, detail="tool dispatch is missing a branch")


async def _call_upload(arguments: dict, ext: core.ExternalCaller, session: AsyncSession) -> dict:
    filename = arguments.get("filename")
    content_b64 = arguments.get("content_base64")
    space = arguments.get("space")
    if not isinstance(filename, str) or not filename.strip():
        return protocol.tool_result("Say the file's name.", is_error=True)
    if not isinstance(content_b64, str) or not content_b64:
        return protocol.tool_result("The file has no content.", is_error=True)
    try:
        data = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError):
        return protocol.tool_result("The file content is not valid base64.", is_error=True)

    limit = _upload_byte_limit()
    # Base64 expands by ~4/3; a pre-check on the encoded string length avoids
    # decoding a deliberately huge payload just to then reject it (mirrors
    # external.py's Content-Length pre-check for the REST multipart path).
    if len(content_b64) > (limit * 4 // 3) + 64 * 1024:
        return protocol.tool_result(
            f"The file is larger than the {limit}-byte limit.", is_error=True
        )
    if len(data) > limit:
        return protocol.tool_result(
            f"The file is larger than the {limit}-byte limit.", is_error=True
        )

    try:
        result = await adapters.do_upload_bytes(
            data=data,
            filename=filename,
            declared_mime="",
            space=space if isinstance(space, str) else None,
            ext=ext,
            session=session,
        )
    except adapters.AdapterHTTPError as exc:
        return protocol.tool_result(formatting.format_error(exc), is_error=True)
    return protocol.tool_result(
        formatting.format_upload_result(result), result.model_dump(mode="json")
    )


def _upload_byte_limit() -> int:
    from quberi_api.services import documents as documents_service

    return documents_service.MAX_UPLOAD_BYTES


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = first.get("msg", "invalid input")
    return f"{loc}: {msg}" if loc else msg


mcp_router = router
