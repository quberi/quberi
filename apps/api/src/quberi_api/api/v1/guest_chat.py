"""Guest demo assistant: let an anonymous visitor ask Quberi about Quberi.

* ``GET  /api/v1/guest/quota``      — how many questions the caller has left today
* ``GET  /api/v1/guest/starters``   — suggested questions, most-asked first
* ``POST /api/v1/guest/ask/stream`` — ask a question, streamed over SSE
  (``locale`` selects the sample space, so answers match the UI language)

Deliberately **stateless**. Guests all share one demo user and one sample
conversation, so persisting their questions would show one visitor's words to
the next and grow the database with anonymous text. Instead the browser sends
the last few turns with each question, the server retrieves from the demo
space and streams an answer, and nothing is written.

Cost is bounded by :mod:`quberi_api.services.guest_limits` (per IP and
global, per day), a question-length cap, a reply-length cap, and a prompt that
confines the assistant to Quberi.

Only guest sessions may call this: signed-in users use the normal
conversation endpoints. It is exempted from the guest read-only guard in
``main.py`` precisely because it never mutates state.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Literal, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.api.v1.spaces.documents import DocumentView
from quberi_api.api.v1.spaces.documents import _serialise as _serialise_doc
from quberi_api.core.config import get_settings
from quberi_api.domain.documents.models import Document
from quberi_api.domain.spaces.models import Space
from quberi_api.infrastructure.qdrant import create_qdrant_client
from quberi_api.services import guest_limits, guest_sandbox, guest_starters
from quberi_api.services.ai.base import ChatMessage
from quberi_api.services.demo_content import (
    GUEST_NOT_READY,
    GUEST_SYSTEM_PROMPT,
    UPLOAD_MARKER_EN,
    UPLOAD_MARKER_ZH,
    locale_for,
)
from quberi_api.services.rag.generate import generate_answer_stream
from quberi_api.services.rag.retrieve import retrieve_for_user, retrieve_with_scope

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/guest", tags=["guest"])

# How many previous turns the model sees. Small on purpose: it bounds the
# prompt (and therefore cost) no matter what the client sends.
_MAX_HISTORY_TURNS = 6
_MAX_HISTORY_CHARS = 1200


class GuestTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    text: str = Field(max_length=4000)


class GuestAskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # The real cap comes from settings; this is just an upper bound so a
    # hostile client can't make us parse a huge body.
    text: str = Field(min_length=1, max_length=2000)
    history: list[GuestTurn] = Field(default_factory=list, max_length=20)
    # The interface language ("en-US", "zh-CN", ...). Picks which sample space
    # is searched, so the answer and its citations are in the visitor's
    # language. Unknown or missing values fall back to English.
    locale: Optional[str] = Field(default=None, max_length=16)
    # Set when the visitor clicked one of the suggested questions (its stable
    # key). Used only to count popularity; unknown keys are ignored.
    starter: Optional[str] = Field(default=None, max_length=32)


class StarterView(BaseModel):
    key: str
    count: int  # asks over the last 30 days
    hot: bool  # among the most-asked, with enough asks to mean something


class StarterList(BaseModel):
    items: list[StarterView]


class GuestQuotaView(BaseModel):
    limit: int  # 0 = uncapped
    used: int
    remaining: int  # -1 = uncapped


def client_ip(request: Request) -> str:
    """Best-effort client address.

    Production sits behind Cloudflare and Caddy, so the socket peer is a
    proxy. ``CF-Connecting-IP`` is set by Cloudflare; ``X-Forwarded-For`` is
    the fallback. Both are client-controllable if someone reaches the origin
    directly, which is why the global daily cap — not this value — is what
    ultimately bounds spend.
    """

    cf = request.headers.get("cf-connecting-ip", "").strip()
    if cf:
        return cf
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if fwd:
        return fwd
    return request.client.host if request.client else "unknown"


def _require_guest(caller: CallerContext) -> None:
    if not caller.is_guest:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the guest assistant is only for guest sessions",
        )


@router.get("/quota", response_model=GuestQuotaView, summary="Guest questions left today")
async def get_quota(
    request: Request,
    caller: CallerContext = Depends(require_caller),
) -> GuestQuotaView:
    _require_guest(caller)
    quota = await guest_limits.peek(get_settings(), ip=client_ip(request))
    return GuestQuotaView(limit=quota.limit, used=quota.used, remaining=quota.remaining)


@router.get(
    "/starters",
    response_model=StarterList,
    summary="Suggested questions, most-asked first",
)
async def list_starters(
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> StarterList:
    _require_guest(caller)
    ranked = await guest_starters.ranking(session)
    return StarterList(
        items=[StarterView(key=r.key, count=r.count, hot=r.hot) for r in ranked]
    )


# ---------------------------------------------------------------------------
# Guest sandbox: upload / delete a file without an account
# ---------------------------------------------------------------------------


def _sandbox_http(exc: guest_sandbox.SandboxError) -> HTTPException:
    codes = {
        "unsupported_type": status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        "too_large": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        "empty": status.HTTP_400_BAD_REQUEST,
        "no_token": status.HTTP_400_BAD_REQUEST,
        "full": status.HTTP_503_SERVICE_UNAVAILABLE,
        "too_many": status.HTTP_429_TOO_MANY_REQUESTS,
    }
    return HTTPException(status_code=codes.get(exc.code, 400), detail=exc.message)


@router.post(
    "/uploads",
    response_model=DocumentView,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file to the guest sandbox (no account)",
)
async def upload_to_sandbox(
    request: Request,
    file: UploadFile = File(...),
    x_guest_token: Optional[str] = Header(default=None),
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> DocumentView:
    """Store a visitor's file in the shared sandbox, tagged with their browser.

    Everything that can reject the request does so **before** any work is
    spent on the file: identity, size, type, then the per-IP and global daily
    caps, then how full the sandbox is. Only then is it stored and queued for
    indexing, exactly like a normal upload.
    """

    _require_guest(caller)
    if not guest_sandbox.valid_token(x_guest_token):
        raise _sandbox_http(
            guest_sandbox.SandboxError(
                "no_token",
                "缺少浏览器标识，请刷新页面重试。 Missing browser id; please reload.",
            )
        )
    token = x_guest_token or ""
    settings = get_settings()

    # Read at most limit+1 bytes: enough to know it is too big, never the whole file.
    raw = await file.read(guest_sandbox.MAX_FILE_BYTES + 1)
    try:
        guest_sandbox.check_upload(
            filename=file.filename or "",
            content_type=file.content_type or "",
            size=len(raw),
        )
    except guest_sandbox.SandboxError as exc:
        raise _sandbox_http(exc) from exc

    try:
        await guest_limits.consume_upload(settings, ip=client_ip(request))
    except guest_limits.GuestLimitError as exc:
        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
                if exc.reason == "unavailable"
                else status.HTTP_429_TOO_MANY_REQUESTS
            ),
            detail=exc.message,
        ) from exc

    org_id = caller.organization.id
    space = await guest_sandbox.get_sandbox_space(session, organization_id=org_id)
    if space is None:
        raise HTTPException(
            status_code=503, detail="体验区暂时不可用。 The sandbox is unavailable."
        )
    space_id = space.id
    mine = await guest_sandbox.owned_documents(session, space_id=space_id, token=token)
    if len(mine) >= guest_sandbox.MAX_FILES_PER_BROWSER:
        raise _sandbox_http(
            guest_sandbox.SandboxError(
                "too_many",
                f"同时最多保留 {guest_sandbox.MAX_FILES_PER_BROWSER} 个文件，请先删除一个。 "
                f"You can keep up to {guest_sandbox.MAX_FILES_PER_BROWSER} files "
                "at once; delete one first.",
            )
        )
    active = await guest_sandbox.count_active(session, space_id=space_id)
    if active >= guest_sandbox.MAX_ACTIVE_DOCUMENTS:
        raise _sandbox_http(
            guest_sandbox.SandboxError(
                "full",
                "体验区已满，请稍后再试。 The sandbox is full; please try again later.",
            )
        )
    await session.commit()

    from io import BytesIO

    from quberi_api.services import documents as documents_service

    view = await documents_service.upload_local(
        session,
        organization_id=org_id,
        space_id=space_id,
        uploader_user_id=caller.user.id,
        filename=(file.filename or "upload").strip(),
        file_obj=BytesIO(raw),
        content_type=file.content_type or "application/octet-stream",
        attributes=guest_sandbox.expiry_attributes(token),
    )
    # Same shape as an ordinary upload, so the web app's list and stats code
    # needs no guest-specific branch. ``is_mine`` is true: it was just uploaded
    # with this browser's token.
    return _serialise_doc(view, token)


@router.delete(
    "/uploads/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a file you uploaded to the guest sandbox",
)
async def delete_from_sandbox(
    document_id: uuid.UUID,
    x_guest_token: Optional[str] = Header(default=None),
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Remove one of your own sandbox files, and check its vectors are gone.

    A file that is not yours - or does not exist - is a plain 404: the endpoint
    must not reveal which files belong to someone else.
    """

    _require_guest(caller)
    not_found = HTTPException(status_code=404, detail="文件不存在。 File not found.")
    if not guest_sandbox.valid_token(x_guest_token):
        raise not_found
    org_id = caller.organization.id
    space = await guest_sandbox.get_sandbox_space(session, organization_id=org_id)
    if space is None:
        raise not_found
    doc = await session.get(Document, document_id)
    if (
        doc is None
        or doc.space_id != space.id
        or not guest_sandbox.is_owned_by(doc.attributes, x_guest_token)
    ):
        raise not_found
    await session.commit()

    from quberi_api.services.sandbox_cleanup import delete_sandbox_document

    await delete_sandbox_document(
        session,
        document_id=document_id,
        space_id=space.id,
        org_id=org_id,
        actor_user_id=caller.user.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _sse(kind: str, data: dict) -> str:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/ask/stream", summary="Ask the guest demo assistant (SSE, stateless)")
async def ask_stream(
    payload: GuestAskRequest,
    request: Request,
    x_guest_token: Optional[str] = Header(default=None),
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    _require_guest(caller)
    settings = get_settings()

    if len(payload.text) > settings.guest_max_question_chars:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"问题请控制在 {settings.guest_max_question_chars} 字以内。 "
                f"Please keep questions under {settings.guest_max_question_chars} characters."
            ),
        )

    # Count the question before doing any work. A rejected request is a
    # clean 429 (not a half-open stream) the client can show as a message.
    try:
        quota = await guest_limits.consume(settings, ip=client_ip(request))
    except guest_limits.GuestLimitError as exc:
        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
                if exc.reason == "unavailable"
                else status.HTTP_429_TOO_MANY_REQUESTS
            ),
            detail=exc.message,
        ) from exc

    # Popularity: count a suggested question only once it has passed the rate
    # limit above, so click-spamming is capped by the same per-IP / global
    # daily limits as asking. A counting failure must never block the answer.
    if payload.starter and guest_starters.is_valid_key(payload.starter):
        try:
            await guest_starters.record(session, payload.starter)
        except Exception:  # noqa: BLE001
            _log.exception("could not record guest starter popularity")

    history = [
        ChatMessage(role=t.role, content=t.text[:_MAX_HISTORY_CHARS])
        for t in payload.history[-_MAX_HISTORY_TURNS:]
    ]
    org_id = caller.organization.id
    user_id = caller.user.id
    identity_id = caller.user.identity_id
    question = payload.text
    # The sample space for the visitor's language (None before the demo
    # content has been synced, in which case we search whatever they can see).
    space_id = (
        await session.execute(
            select(Space.id).where(
                Space.organization_id == org_id,
                Space.slug == locale_for(payload.locale).space_slug,
            )
        )
    ).scalar_one_or_none()

    # The visitor's own sandbox uploads (by browser token), if any. Resolved
    # before streaming; an empty list means "nothing of theirs to search".
    sandbox_space_id = None
    own_doc_ids: list[uuid.UUID] = []
    if guest_sandbox.valid_token(x_guest_token):
        sandbox = await guest_sandbox.get_sandbox_space(session, organization_id=org_id)
        if sandbox is not None:
            sandbox_space_id = sandbox.id
            mine = await guest_sandbox.owned_documents(
                session, space_id=sandbox.id, token=x_guest_token or ""
            )
            own_doc_ids = [d.id for d in mine]
    marker = UPLOAD_MARKER_ZH if locale_for(payload.locale).locale == "zh-CN" else UPLOAD_MARKER_EN
    await session.commit()

    async def events() -> AsyncIterator[str]:
        yield _sse("quota", {"limit": quota.limit, "remaining": quota.remaining})
        qdrant = create_qdrant_client(settings)
        try:
            # Only the visitor's-language space, and only if the guest can
            # actually see it ("intersect" keeps the permission check).
            if space_id is not None:
                retrieved = await retrieve_with_scope(
                    session,
                    qdrant=qdrant,
                    organization_id=org_id,
                    caller_user_id=user_id,
                    query=question,
                    scope_mode="intersect",
                    scope_space_ids=[space_id],
                )
            else:
                retrieved = await retrieve_for_user(
                    session,
                    qdrant=qdrant,
                    organization_id=org_id,
                    caller_user_id=user_id,
                    query=question,
                )
            chunks = list(retrieved.chunks)
            if own_doc_ids and sandbox_space_id is not None:
                from dataclasses import replace

                own = await retrieve_with_scope(
                    session,
                    qdrant=qdrant,
                    organization_id=org_id,
                    caller_user_id=user_id,
                    query=question,
                    scope_mode="intersect",
                    scope_space_ids=[sandbox_space_id],
                    top_k=4,
                    document_ids=own_doc_ids,
                )
                # Tag every excerpt of a visitor's file so the model (rule 9)
                # and the citation list can tell it apart from product docs.
                chunks += [
                    replace(c, document_name=f"{marker}{c.document_name}")
                    for c in own.chunks
                ]
            if not chunks:
                # Nothing indexed yet (e.g. right after a deploy): say so
                # without spending a model call.
                yield _sse("retrieved", {"model_id": "", "citations": []})
                yield _sse("delta", {"text": GUEST_NOT_READY})
                yield _sse(
                    "done",
                    {
                        "answer": GUEST_NOT_READY,
                        "citations": [],
                        "model_id": "",
                        "system_note": "not_ready",
                    },
                )
                return
            async for event in generate_answer_stream(
                session,
                organization_id=org_id,
                identity_id=identity_id,
                query=question,
                chunks=chunks,
                history=history,
                system_prompt=GUEST_SYSTEM_PROMPT,
                max_tokens=settings.guest_max_answer_tokens,
            ):
                yield _sse(event.kind, event.data)
        except Exception as exc:  # noqa: BLE001 — surface as an SSE error
            _log.exception("guest ask failed")
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})
            yield _sse("done", {})
        finally:
            await qdrant.close()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


guest_chat_router = router
