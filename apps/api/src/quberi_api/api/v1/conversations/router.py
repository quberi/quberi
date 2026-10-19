"""Conversation & message endpoints.

Routes
------
* GET  /api/v1/spaces/{space_id}/conversations
* POST /api/v1/spaces/{space_id}/conversations
* GET  /api/v1/conversations/{conversation_id}/messages
* POST /api/v1/conversations/{conversation_id}/messages
* POST /api/v1/conversations/{conversation_id}/image-search
* GET  /api/v1/conversations/{conversation_id}/messages/{message_id}/picture

Every endpoint hangs off ``require_caller`` and the service layer checks
that the caller can see the conversation's Space. When a caller cannot see
the target, we return 404 rather than 403 so we do not leak the existence
of things they should not know about.
"""

from __future__ import annotations

import uuid

import json

from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session, get_infrastructure
from quberi_api.api.security import CallerContext, require_caller
from quberi_api.api.v1.conversations.schemas import (
    AddScopeSourceRequest,
    ConversationList,
    ConversationSummary,
    CreateConversationRequest,
    MessageList,
    MessageSummary,
    ReviewEditsRequest,
    ScopeSourceList,
    ScopeSourceView,
    SendMessageRequest,
    SendMessageResponse,
    SetConversationAgentRequest,
    SetConversationModelRequest,
    SetConversationTitleRequest,
    SetScopeModeRequest,
)
from quberi_api.domain.conversations.models import Conversation, Message
from quberi_api.infrastructure.registry import Infrastructure
from quberi_api.services import conversation_pictures
from quberi_api.services import conversations as conversations_service
from quberi_api.services import send_review
from quberi_api.services import usage as usage_service
from quberi_api.services.ai.local_image_embedding import ImageDecodeError
from quberi_api.services.oauth import core as oauth_core
from quberi_api.services.rag.image_search import ImageSearchDisabled, ImageSearchError

space_scoped_router = APIRouter(prefix="/spaces", tags=["conversations"])
conversation_router = APIRouter(prefix="/conversations", tags=["conversations"])


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _summary(conversation: Conversation, *, is_owner: bool) -> ConversationSummary:
    attrs = conversation.attributes or {}
    model_choice = attrs.get("model")
    agent_choice = attrs.get("agent")
    return ConversationSummary.model_validate(conversation).model_copy(
        update={
            "is_owner": is_owner,
            "model_choice": model_choice if isinstance(model_choice, dict) else None,
            "agent_choice": agent_choice if isinstance(agent_choice, dict) else None,
        }
    )


def _message_summary(message: Message, content: dict | None = None) -> MessageSummary:
    return MessageSummary(
        id=message.id,
        conversation_id=message.conversation_id,
        role=message.role,
        content=message.content if content is None else content,
        author_user_id=message.author_user_id,
        status=message.status,
        correlation_id=message.correlation_id,
        created_at=message.created_at,
    )


# ---------------------------------------------------------------------------
# Space-scoped: list / create
# ---------------------------------------------------------------------------


@space_scoped_router.get(
    "/{space_id}/conversations",
    response_model=ConversationList,
    summary="List conversations in a space",
)
async def list_conversations(
    space_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ConversationList:
    views = await conversations_service.list_conversations_for_space(
        session,
        space_id=space_id,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
    )
    items = [_summary(v.conversation, is_owner=v.is_owner) for v in views]
    return ConversationList(items=items, total=len(items))


@space_scoped_router.post(
    "/{space_id}/conversations",
    status_code=status.HTTP_201_CREATED,
    response_model=ConversationSummary,
    summary="Create an empty conversation in a space",
)
async def create_conversation(
    space_id: uuid.UUID,
    payload: CreateConversationRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ConversationSummary:
    view = await conversations_service.create_conversation(
        session,
        space_id=space_id,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
        title=payload.title,
    )
    return _summary(view.conversation, is_owner=view.is_owner)


# ---------------------------------------------------------------------------
# Conversation-scoped: list messages / send message
# ---------------------------------------------------------------------------


@conversation_router.get(
    "/{conversation_id}/messages",
    response_model=MessageList,
    summary="List messages in a conversation",
)
async def list_messages(
    conversation_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> MessageList:
    conversation, messages = await conversations_service.list_messages(
        session,
        conversation_id=conversation_id,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
    )
    is_owner = conversation.created_by_user_id == caller.user.id
    contents = await conversations_service.contents_for_reader(
        session,
        messages=messages,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
    )
    return MessageList(
        conversation=_summary(conversation, is_owner=is_owner),
        items=[_message_summary(m, contents[m.id]) for m in messages],
        total=len(messages),
    )


@conversation_router.post(
    "/{conversation_id}/messages",
    status_code=status.HTTP_201_CREATED,
    response_model=SendMessageResponse,
    summary="Send a message and receive the assistant reply",
)
async def send_message(
    conversation_id: uuid.UUID,
    payload: SendMessageRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> SendMessageResponse:
    if payload.review:
        # Only the streaming endpoint can pause for a review; ignoring the flag
        # here would send the content unchecked.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="review before sending needs the streaming endpoint",
        )
    # A question about pictures is answered from the picture index with no model, so it
    # neither spends the free allowance nor needs a held prompt.
    picture_plan = (
        await conversations_service.plan_picture_turn(
            session,
            conversation_id=conversation_id,
            organization_id=caller.organization.id,
            user_id=caller.user.id,
            text=payload.text,
        )
        if payload.pictures
        else None
    )
    if picture_plan is None:
        await usage_service.enforce_free_quota(
            session,
            organization_id=caller.organization.id,
            tier=caller.organization.tier,
        )
    pair = await conversations_service.send_message(
        session,
        conversation_id=conversation_id,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
        identity_id=caller.user.identity_id,
        text=payload.text,
        picture_plan=picture_plan,
        with_pictures=payload.pictures,
    )
    is_owner = pair.conversation.created_by_user_id == caller.user.id
    return SendMessageResponse(
        conversation=_summary(pair.conversation, is_owner=is_owner),
        user_message=_message_summary(pair.user_message),
        assistant_message=_message_summary(pair.assistant_message),
    )


# A picture used as a query. Generous for a photo, small enough that a request cannot
# tie the server up; the decoder also enforces a pixel limit.
_MAX_QUERY_PICTURE_BYTES = 10 * 1024 * 1024
_IMAGE_SEARCH_PER_MINUTE = 20


async def _read_picture(upload: UploadFile) -> bytes:
    """Read the uploaded query picture in pieces, stopping as soon as it is too big."""

    buf = bytearray()
    while True:
        piece = await upload.read(1024 * 1024)
        if not piece:
            return bytes(buf)
        buf.extend(piece)
        if len(buf) > _MAX_QUERY_PICTURE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="the picture is larger than 10 MB",
            )


@conversation_router.post(
    "/{conversation_id}/image-search",
    status_code=status.HTTP_201_CREATED,
    response_model=SendMessageResponse,
    summary="Find similar pictures from a picture and/or a description",
)
async def image_search_message(
    conversation_id: uuid.UUID,
    file: Optional[UploadFile] = File(default=None),
    text: Optional[str] = Form(default=None, max_length=500),
    top_k: int = Form(default=12, ge=1, le=30),
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
    infra: Infrastructure = Depends(get_infrastructure),
) -> SendMessageResponse:
    """Search the pictures this conversation may see.

    The answer is stored as an ordinary assistant message whose content carries
    ``image_matches``. No language model is called. The query picture is kept for its
    sender (``GET .../messages/{message_id}/picture``) once the search has worked.
    """

    try:
        await oauth_core.rate_limit(
            infra.redis,
            key=f"quberi:imagesearch:{caller.user.id}",
            limit=_IMAGE_SEARCH_PER_MINUTE,
            window_seconds=60,
        )
    except oauth_core.OAuthError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail="too many picture searches; wait a minute",
            headers={"Retry-After": "60"} if exc.status == 429 else None,
        ) from exc
    picture = await _read_picture(file) if file is not None else None
    try:
        pair = await conversations_service.send_image_search(
            session,
            conversation_id=conversation_id,
            organization_id=caller.organization.id,
            user_id=caller.user.id,
            image=picture,
            text=text,
            top_k=top_k,
            filename=file.filename if file is not None else None,
        )
    except ImageSearchDisabled as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except (ImageDecodeError, ImageSearchError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    is_owner = pair.conversation.created_by_user_id == caller.user.id
    return SendMessageResponse(
        conversation=_summary(pair.conversation, is_owner=is_owner),
        user_message=_message_summary(pair.user_message),
        assistant_message=_message_summary(pair.assistant_message),
    )


@conversation_router.get(
    "/{conversation_id}/messages/{message_id}/picture",
    summary="The picture a question was searched with",
    responses={200: {"content": {"image/png": {}, "image/jpeg": {}, "image/gif": {}, "image/webp": {}}}},
)
async def message_picture(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    """Give a picture back to the person who searched with it, and to no one else.

    Seeing the conversation is not enough: another member who can read the thread gets a
    plain 404. The type sent is the one found when the picture was kept (never the
    upload's own label), with ``nosniff`` and a policy that forbids running anything.
    """

    await conversations_service.get_conversation_for_user(
        session,
        conversation_id=conversation_id,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
    )
    row = await conversation_pictures.find_for_owner(
        session,
        conversation_id=conversation_id,
        message_id=message_id,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
    )
    return StreamingResponse(
        conversation_pictures.open_stream(row),
        media_type=row.mime_type,
        headers={
            "Content-Length": str(row.size_bytes),
            "Content-Disposition": "inline",
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@conversation_router.post(
    "/{conversation_id}/messages/stream",
    summary="Send a message and stream the assistant reply as SSE",
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": (
                "Server-Sent Events with kinds: user_message, retrieved, "
                "delta, error, assistant_message, done."
            ),
        }
    },
)
async def send_message_stream(
    conversation_id: uuid.UUID,
    payload: SendMessageRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    """Stream a conversation reply via Server-Sent Events.

    Free-quota gate runs before the stream opens so an exhausted allowance
    returns a clean 402 rather than a half-started SSE.

    Each event has the wire form::

        event: <kind>
        data: <json>
        \n

    Clients consume via ``fetch`` + ``ReadableStream`` (``EventSource``
    only supports GET, so we hand-roll the framing on the POST path).
    """

    # A question about pictures is answered from the picture index with no model:
    # no free allowance is spent and nothing is held for review, whatever the
    # "check before sending" switch says (there is nothing to check: no text goes out).
    picture_plan = (
        await conversations_service.plan_picture_turn(
            session,
            conversation_id=conversation_id,
            organization_id=caller.organization.id,
            user_id=caller.user.id,
            text=payload.text,
        )
        if payload.pictures
        else None
    )
    # A held prompt calls no model yet, so the free allowance is reserved when
    # it is approved, not here (a cancelled review must not use any up).
    if picture_plan is None and not payload.review:
        await usage_service.enforce_free_quota(
            session,
            organization_id=caller.organization.id,
            tier=caller.organization.tier,
        )

    async def event_source():
        try:
            async for kind, data in conversations_service.send_message_stream(
                session,
                conversation_id=conversation_id,
                organization_id=caller.organization.id,
                user_id=caller.user.id,
                identity_id=caller.user.identity_id,
                text=payload.text,
                review=payload.review,
                picture_plan=picture_plan,
                with_pictures=payload.pictures,
            ):
                payload_str = json.dumps(data, ensure_ascii=False)
                yield f"event: {kind}\ndata: {payload_str}\n\n"
        except Exception as exc:  # noqa: BLE001 — surface as SSE error
            msg = f"{type(exc).__name__}: {exc}"
            yield (
                "event: error\n"
                f"data: {json.dumps({'message': msg}, ensure_ascii=False)}\n\n"
            )
            yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Nginx buffers SSE by default; disable for proxied deploys.
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Review before sending
# ---------------------------------------------------------------------------


def _review_edits(payload: ReviewEditsRequest) -> send_review.ReviewEdits:
    return send_review.ReviewEdits(
        question=payload.question,
        history=payload.history,
        references=[
            send_review.ReferenceEdit(index=r.index, include=r.include, text=r.text)
            for r in payload.references
        ],
    )


@conversation_router.post(
    "/{conversation_id}/messages/review/{pending_id}/preview",
    summary="Show the held prompt with the person's edits (nothing is sent)",
)
async def preview_review(
    conversation_id: uuid.UUID,
    pending_id: str,
    payload: ReviewEditsRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    return await conversations_service.preview_review(
        session,
        conversation_id=conversation_id,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
        pending_id=pending_id,
        edits=_review_edits(payload),
    )


@conversation_router.post(
    "/{conversation_id}/messages/review/{pending_id}/cancel",
    summary="Cancel a held prompt; the model is never called",
)
async def cancel_review(
    conversation_id: uuid.UUID,
    pending_id: str,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    return await conversations_service.cancel_review(
        session,
        conversation_id=conversation_id,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
        pending_id=pending_id,
    )


@conversation_router.post(
    "/{conversation_id}/messages/review/{pending_id}/approve",
    summary="Send the reviewed prompt to the model and stream the reply as SSE",
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def approve_review(
    conversation_id: uuid.UUID,
    pending_id: str,
    payload: ReviewEditsRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    await usage_service.enforce_free_quota(
        session,
        organization_id=caller.organization.id,
        tier=caller.organization.tier,
    )
    edits = _review_edits(payload)
    # Fail with a real 404/422 before the stream opens (after that the status
    # is fixed at 200 and a bad edit could only be reported inside the stream).
    await conversations_service.check_review_edits(
        session,
        conversation_id=conversation_id,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
        pending_id=pending_id,
        edits=edits,
    )
    await session.commit()

    async def event_source():
        try:
            async for kind, data in conversations_service.resume_reviewed_stream(
                session,
                conversation_id=conversation_id,
                organization_id=caller.organization.id,
                user_id=caller.user.id,
                identity_id=caller.user.identity_id,
                pending_id=pending_id,
                edits=edits,
            ):
                payload_str = json.dumps(data, ensure_ascii=False)
                yield f"event: {kind}\ndata: {payload_str}\n\n"
        except Exception as exc:  # noqa: BLE001 - surface as SSE error
            msg = f"{type(exc).__name__}: {exc}"
            yield (
                "event: error\n"
                f"data: {json.dumps({'message': msg}, ensure_ascii=False)}\n\n"
            )
            yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Knowledge scope (Slice F)
# ---------------------------------------------------------------------------


def _scope_source_view(v: conversations_service.KnowledgeSourceView) -> ScopeSourceView:
    link = v.link
    return ScopeSourceView(
        id=link.id,
        conversation_id=link.conversation_id,
        space_id=v.space_id,
        space_name=v.space_name,
        space_slug=v.space_slug,
        hidden=v.hidden,
        added_by_user_id=link.added_by_user_id,
        created_at=link.created_at,
    )


@conversation_router.patch(
    "/{conversation_id}/scope-mode",
    response_model=ConversationSummary,
    summary="Switch a conversation between auto and custom knowledge scope",
)
async def set_scope_mode(
    conversation_id: uuid.UUID,
    payload: SetScopeModeRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ConversationSummary:
    conv = await conversations_service.set_scope_mode(
        session,
        conversation_id=conversation_id,
        organization_id=caller.organization.id,
        caller_user_id=caller.user.id,
        mode=payload.mode,
    )
    is_owner = conv.created_by_user_id == caller.user.id
    return _summary(conv, is_owner=is_owner)


@conversation_router.patch(
    "/{conversation_id}/model",
    response_model=ConversationSummary,
    summary="Set or clear the conversation's model override",
)
async def set_conversation_model(
    conversation_id: uuid.UUID,
    payload: SetConversationModelRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ConversationSummary:
    conv = await conversations_service.set_conversation_model(
        session,
        conversation_id=conversation_id,
        organization_id=caller.organization.id,
        caller_user_id=caller.user.id,
        caller_identity_id=caller.user.identity_id,
        model_id=payload.model_id,
        binding_id=payload.binding_id,
    )
    is_owner = conv.created_by_user_id == caller.user.id
    return _summary(conv, is_owner=is_owner)


@conversation_router.patch(
    "/{conversation_id}/title",
    response_model=ConversationSummary,
    summary="Rename a conversation",
)
async def set_conversation_title(
    conversation_id: uuid.UUID,
    payload: SetConversationTitleRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ConversationSummary:
    conv = await conversations_service.rename_conversation(
        session,
        conversation_id=conversation_id,
        organization_id=caller.organization.id,
        caller_user_id=caller.user.id,
        title=payload.title,
    )
    is_owner = conv.created_by_user_id == caller.user.id
    return _summary(conv, is_owner=is_owner)


@conversation_router.patch(
    "/{conversation_id}/agent",
    response_model=ConversationSummary,
    summary="Bind or clear the conversation's agent",
)
async def set_conversation_agent(
    conversation_id: uuid.UUID,
    payload: SetConversationAgentRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ConversationSummary:
    conv = await conversations_service.set_conversation_agent(
        session,
        conversation_id=conversation_id,
        organization_id=caller.organization.id,
        caller_user_id=caller.user.id,
        agent_id=payload.agent_id,
    )
    is_owner = conv.created_by_user_id == caller.user.id
    return _summary(conv, is_owner=is_owner)


@conversation_router.get(
    "/{conversation_id}/scope-sources",
    response_model=ScopeSourceList,
    summary="List additional knowledge-source spaces for a conversation",
)
async def list_scope_sources(
    conversation_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ScopeSourceList:
    views = await conversations_service.list_scope_sources(
        session,
        conversation_id=conversation_id,
        organization_id=caller.organization.id,
        caller_user_id=caller.user.id,
    )
    # Fetch scope_mode via a single extra read — we already validated
    # access inside ``list_scope_sources`` so this is just a metadata pull.
    conv = await conversations_service._load_conversation_for_user(
        session,
        conversation_id=conversation_id,
        organization_id=caller.organization.id,
        user_id=caller.user.id,
    )
    return ScopeSourceList(
        conversation_id=conversation_id,
        scope_mode=conv.scope_mode,
        items=[_scope_source_view(v) for v in views],
        total=len(views),
    )


@conversation_router.post(
    "/{conversation_id}/scope-sources",
    status_code=status.HTTP_201_CREATED,
    response_model=ScopeSourceView,
    summary="Attach an additional space as a knowledge source",
)
async def add_scope_source(
    conversation_id: uuid.UUID,
    payload: AddScopeSourceRequest,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> ScopeSourceView:
    view = await conversations_service.add_scope_source(
        session,
        conversation_id=conversation_id,
        space_id=payload.space_id,
        organization_id=caller.organization.id,
        caller_user_id=caller.user.id,
    )
    return _scope_source_view(view)


@conversation_router.delete(
    "/{conversation_id}/scope-sources/{space_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Detach a knowledge-source space",
)
async def remove_scope_source(
    conversation_id: uuid.UUID,
    space_id: uuid.UUID,
    caller: CallerContext = Depends(require_caller),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await conversations_service.remove_scope_source(
        session,
        conversation_id=conversation_id,
        space_id=space_id,
        organization_id=caller.organization.id,
        caller_user_id=caller.user.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
