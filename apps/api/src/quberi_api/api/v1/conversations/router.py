"""Conversation & message endpoints.

Routes
------
* GET  /api/v1/spaces/{space_id}/conversations
* POST /api/v1/spaces/{space_id}/conversations
* GET  /api/v1/conversations/{conversation_id}/messages
* POST /api/v1/conversations/{conversation_id}/messages

Every endpoint hangs off ``require_caller`` and the service layer checks
that the caller can see the conversation's Space. When a caller cannot see
the target, we return 404 rather than 403 so we do not leak the existence
of things they should not know about.
"""

from __future__ import annotations

import uuid

import json

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.api.dependencies import get_db_session
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
from quberi_api.services import conversations as conversations_service
from quberi_api.services import send_review
from quberi_api.services import usage as usage_service

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


def _message_summary(message: Message) -> MessageSummary:
    return MessageSummary(
        id=message.id,
        conversation_id=message.conversation_id,
        role=message.role,
        content=message.content,
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
    return MessageList(
        conversation=_summary(conversation, is_owner=is_owner),
        items=[_message_summary(m) for m in messages],
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
    )
    is_owner = pair.conversation.created_by_user_id == caller.user.id
    return SendMessageResponse(
        conversation=_summary(pair.conversation, is_owner=is_owner),
        user_message=_message_summary(pair.user_message),
        assistant_message=_message_summary(pair.assistant_message),
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

    # A held prompt calls no model yet, so the free allowance is reserved when
    # it is approved, not here (a cancelled review must not use any up).
    if not payload.review:
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
