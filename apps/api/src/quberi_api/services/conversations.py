"""Conversation & message service.

Scope for this slice
--------------------
* ``create_conversation`` — provisions an empty thread under a Space the
  caller can access. Records an audit event.
* ``send_message`` — appends the caller's user message and, in the same
  transaction, a **placeholder assistant echo**. Real LLM integration lands
  with the RAG slice; the shape of the return value stays stable across
  that swap.
* ``list_conversations_for_space`` — accessible conversations, newest
  activity first.
* ``list_messages`` — chronological messages in a conversation.

Access model: every read/write validates that the caller can see the
conversation's Space via ``spaces.get_space_for_user``. That helper already
enforces the tenant boundary and the personal-space / role-binding
visibility rules.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.core.config import get_settings
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.conversations.models import (
    Conversation,
    ConversationKnowledgeSource,
    Message,
)
from quberi_api.domain.spaces.models import Space
from quberi_api.infrastructure.qdrant import create_qdrant_client
from quberi_api.infrastructure.redis import create_redis_client
from quberi_api.services import send_review
from quberi_api.services import spaces as spaces_service
from quberi_api.services.ai.base import ChatMessage as LlmMessage
from quberi_api.services.authz import (
    list_accessible_space_ids,
    resolve_effective_role_codes,
)
from quberi_api.services import conversation_pictures
from quberi_api.services.doc_acl import load_doc_access
from quberi_api.services.errors import (
    AlreadyExistsError,
    InvalidInputError,
    NotFoundError,
)
from quberi_api.services.rag.generate import (
    GenerationResult,
    StreamEvent,
    describe_chat_model,
    generate_answer,
    generate_answer_stream,
    outgoing_messages,
)
from quberi_api.services.rag.retrieve import (
    effective_access_for_conversation,
    retrieve_for_conversation,
)
from quberi_api.services.retrieval_audit import (
    document_ids_from_chunks,
    document_ids_from_trace,
)

# ---------------------------------------------------------------------------
# Result payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConversationView:
    """One conversation row (list rendering)."""

    conversation: Conversation
    is_owner: bool


@dataclass(frozen=True)
class MessagePair:
    """The user message + the assistant response created together."""

    user_message: Message
    assistant_message: Message
    conversation: Conversation


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_MESSAGE_TEXT = 8_000
_TITLE_MAX = 60
_ECHO_QUOTE_MAX = 80


def _placeholder_assistant_reply(user_text: str) -> str:
    """Canned assistant response — swapped out when the RAG slice lands."""

    snippet = user_text.strip().splitlines()[0][:_ECHO_QUOTE_MAX] if user_text else ""
    if len(user_text) > _ECHO_QUOTE_MAX:
        snippet += "…"
    return (
        "[Placeholder assistant] I received: "
        f"“{snippet}”. Real chat integration ships with the RAG slice."
    )


def _extract_text(content: dict) -> str:
    """Pull plain text out of a ``content`` payload; empty string when absent."""

    text = content.get("text") if isinstance(content, dict) else None
    return text if isinstance(text, str) else ""


def _agent_binding(conversation: Conversation) -> uuid.UUID | None:
    """Read the per-conversation agent binding from ``attributes.agent``.

    Returns the bound agent id, or ``None`` when the conversation runs as
    plain RAG (no agent).
    """
    a = (conversation.attributes or {}).get("agent")
    if isinstance(a, dict) and a.get("agent_id"):
        try:
            return uuid.UUID(str(a["agent_id"]))
        except (ValueError, TypeError):
            return None
    return None


def _model_override(conversation: Conversation) -> tuple[str | None, uuid.UUID | None]:
    """Read a per-conversation model choice from ``attributes.model``.

    Returns ``(model_id, binding_id)`` or ``(None, None)`` when the
    conversation uses the org default.
    """

    m = (conversation.attributes or {}).get("model")
    if isinstance(m, dict) and m.get("model_id") and m.get("binding_id"):
        try:
            return str(m["model_id"]), uuid.UUID(str(m["binding_id"]))
        except (ValueError, TypeError):
            return None, None
    return None, None


async def _effective_override(
    session: AsyncSession,
    *,
    conversation: Conversation,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[str | None, uuid.UUID | None]:
    """The conversation's model override, dropped if the caller is no
    longer allowed to use it (governance) — falls back to org default."""

    mid, bid = _model_override(conversation)
    if not mid or not bid:
        return None, None
    from quberi_api.services import model_access as model_access_service

    allowed = await model_access_service.is_user_allowed(
        session,
        organization_id=organization_id,
        user_id=user_id,
        binding_id=bid,
        model_id=mid,
    )
    return (mid, bid) if allowed else (None, None)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_conversations_for_space(
    session: AsyncSession,
    *,
    space_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> list[ConversationView]:
    # Access check reuses the space visibility logic; a caller with no access
    # sees zero rows (via the raise → 404 the endpoint catches).
    space_view = await spaces_service.get_space_for_user(
        session,
        space_id=space_id,
        organization_id=organization_id,
        user_id=user_id,
    )

    stmt = (
        select(Conversation)
        .where(Conversation.space_id == space_view.space.id)
        .where(Conversation.organization_id == organization_id)
        .order_by(
            Conversation.last_message_at.desc().nullslast(),
            Conversation.created_at.desc(),
        )
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        ConversationView(
            conversation=row,
            is_owner=row.created_by_user_id == user_id,
        )
        for row in rows
    ]


async def get_conversation_for_user(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> ConversationView:
    stmt = (
        select(Conversation)
        .where(Conversation.id == conversation_id)
        .where(Conversation.organization_id == organization_id)
    )
    conversation = (await session.execute(stmt)).scalar_one_or_none()
    if conversation is None:
        raise NotFoundError(f"conversation not found: {conversation_id}")

    # Re-validate space access. If the caller has lost access to the space
    # since the conversation was created (e.g. binding revoked), 404 rather
    # than 403 so we do not leak existence.
    await spaces_service.get_space_for_user(
        session,
        space_id=conversation.space_id,
        organization_id=organization_id,
        user_id=user_id,
    )

    return ConversationView(
        conversation=conversation,
        is_owner=conversation.created_by_user_id == user_id,
    )


async def list_messages(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[Conversation, list[Message]]:
    view = await get_conversation_for_user(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=user_id,
    )
    stmt = (
        select(Message)
        .where(Message.conversation_id == view.conversation.id)
        .order_by(Message.created_at.asc(), Message.id.asc())
    )
    messages = list((await session.execute(stmt)).scalars().all())
    return view.conversation, messages


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


async def create_conversation(
    session: AsyncSession,
    *,
    space_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    title: str | None = None,
) -> ConversationView:
    """Provision an empty conversation in an accessible space."""

    normalised_title = (title or "").strip() or None

    async with session.begin():
        # Run the access check inside the transaction so the SELECT does not
        # open a separate implicit txn that would collide with our own begin.
        space_view = await spaces_service.get_space_for_user(
            session,
            space_id=space_id,
            organization_id=organization_id,
            user_id=user_id,
        )
        conversation = Conversation(
            organization_id=organization_id,
            space_id=space_view.space.id,
            created_by_user_id=user_id,
            title=normalised_title,
            status="active",
            last_message_at=None,
            message_count=0,
            attributes={},
        )
        session.add(conversation)
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=user_id,
                action="conversation.created",
                resource_type="conversation",
                space_id=space_view.space.id,
                payload={"space_slug": space_view.space.slug},
            )
        )
        await session.flush()
        # ``resource_id`` on the audit event points at the just-created
        # conversation. Set it now that we have its id.
        # (Alternatively we could pass id ahead of flush; this is simpler.)
        await session.refresh(conversation, attribute_names=["created_at", "updated_at"])

    return ConversationView(conversation=conversation, is_owner=True)


async def send_message(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    identity_id: uuid.UUID | None = None,
    text: str,
    picture_plan: PicturePlan | None = None,
    with_pictures: bool = True,
) -> MessagePair:
    """Append a user message and generate a RAG-grounded assistant reply.

    Steps
    -----
    1. Access check + record the user message (transaction A).
    2. Load short recent history for multi-turn coherence.
    3. Retrieve top-K chunks scoped to the caller's live-accessible
       spaces (intersected with the conversation's ``scope_mode``).
    4. Call the org's configured chat provider with the numbered
       references prompt.
    5. Persist the assistant message with citations in
       ``content['citations']`` (transaction B).

    Errors during retrieval or generation don't roll back the user
    message; instead we persist an assistant message with a
    ``placeholder: true`` flag and an error note so the user can retry.
    """

    text = text.strip() if isinstance(text, str) else ""
    if not text:
        raise InvalidInputError("message text is required")
    if len(text) > _MAX_MESSAGE_TEXT:
        raise InvalidInputError(
            f"message text exceeds {_MAX_MESSAGE_TEXT} character limit"
        )

    if picture_plan is not None:
        # A picture question, already answered from the picture index (no model).
        return await _answer_picture_plan(
            session,
            plan=picture_plan,
            conversation_id=conversation_id,
            organization_id=organization_id,
            user_id=user_id,
        )

    correlation_id = uuid.uuid4().hex

    # ---- Transaction A: user message + access check ----
    async with session.begin():
        view = await get_conversation_for_user(
            session,
            conversation_id=conversation_id,
            organization_id=organization_id,
            user_id=user_id,
        )
        conversation = view.conversation

        user_message = Message(
            conversation_id=conversation.id,
            organization_id=organization_id,
            role="user",
            content={"type": "text", "text": text},
            author_user_id=user_id,
            tokens={},
            cost={},
            correlation_id=correlation_id,
            status="completed",
        )
        session.add(user_message)
        await session.flush()
        await session.refresh(user_message, attribute_names=["created_at"])

    # ---- Load short history (pre-retrieval, outside a write txn) ----
    history_msgs = await _recent_history_for_llm(
        session,
        conversation_id=conversation.id,
        exclude_message_id=user_message.id,
    )

    # ---- Generation: agent tool-loop when bound, else plain RAG ----
    agent_id = _agent_binding(conversation)
    citations_payload: list[dict] = []
    assistant_meta: dict = {}
    error_note: str | None = None
    # Ids of every document retrieval handed to the model (audit only).
    retrieved_doc_ids: list[str] = []

    if agent_id is not None:
        from quberi_api.services.agents import config as agents_config
        from quberi_api.services.agents.external import run_external_agent
        from quberi_api.services.agents.runner import run_agent

        try:
            agent_obj = await agents_config.get_agent(
                session, organization_id=organization_id, agent_id=agent_id
            )
            if agent_obj.kind == "external":
                result = await run_external_agent(
                    session,
                    agent=agent_obj,
                    user_id=user_id,
                    query=text,
                    history=history_msgs,
                )
            else:
                result = await run_agent(
                    session,
                    organization_id=organization_id,
                    user_id=user_id,
                    query=text,
                    agent_id=agent_id,
                    history=history_msgs,
                )
            assistant_text = result.answer
            assistant_meta = {
                "model_id": result.model_id,
                "agent_id": str(agent_id),
                "iterations": result.iterations,
                "tool_call_count": result.tool_call_count,
                "trace": [s.to_dict() for s in result.trace],
            }
            retrieved_doc_ids = document_ids_from_trace(assistant_meta["trace"])
            assistant_placeholder = False
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).exception(
                "agent run failed for conversation %s", conversation.id
            )
            error_note = f"{type(exc).__name__}: {exc}"
            assistant_text = (
                "智能体运行时发生错误：稍后再试，或让管理员检查智能体配置与模型设置。"
            )
            assistant_meta = {"error": error_note, "agent_id": str(agent_id)}
            assistant_placeholder = True
    else:
        generation: GenerationResult
        qdrant = create_qdrant_client(get_settings())
        try:
            retrieved = await retrieve_for_conversation(
                session,
                qdrant=qdrant,
                conversation=conversation,
                caller_user_id=user_id,
                query=text,
            )
            ov_model, ov_binding = await _effective_override(
                session,
                conversation=conversation,
                organization_id=organization_id,
                user_id=user_id,
            )
            retrieved_doc_ids = document_ids_from_chunks(retrieved.chunks)
            generation = await generate_answer(
                session,
                organization_id=organization_id,
                identity_id=identity_id,
                query=text,
                chunks=retrieved.chunks,
                history=history_msgs,
                model_id=ov_model,
                binding_id=ov_binding,
                scope_is_empty=len(retrieved.space_ids) == 0,
            )
            citations_payload = [
                {
                    "index": c.index,
                    "document_id": str(c.document_id),
                    "space_id": str(c.space_id),
                    "document_name": c.document_name,
                    "chunk_index": c.chunk_index,
                    "text": c.text,
                    "score": c.score,
                    "used": c.used,
                    "char_start": c.char_start,
                    "char_end": c.char_end,
                }
                for c in generation.citations
            ]
            assistant_meta = {
                "model_id": generation.model_id,
                "prompt_tokens": generation.prompt_tokens,
                "completion_tokens": generation.completion_tokens,
                "scope_mode": retrieved.scope_mode,
                "scope_space_ids": [str(s) for s in retrieved.space_ids],
                "retrieved_count": len(retrieved.chunks),
                "system_note": generation.system_note,
            }
            assistant_text = generation.answer
            assistant_placeholder = False
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).exception(
                "RAG generation failed for conversation %s", conversation.id
            )
            error_note = f"{type(exc).__name__}: {exc}"
            assistant_text = (
                "生成回答时发生错误：稍后再试，或让管理员在\"AI 供应商\"设置里"
                "检查密钥与模型选择。"
            )
            assistant_meta = {"error": error_note}
            assistant_placeholder = True
        finally:
            await qdrant.close()

    related: list[dict] = []
    if with_pictures and agent_id is None and error_note is None:
        related = await related_pictures(
            session,
            conversation_id=conversation.id,
            organization_id=organization_id,
            user_id=user_id,
            text=text,
        )

    # Every retrieval SELECT auto-began a transaction on this session;
    # commit it before opening a fresh write transaction below (the
    # familiar "already begun" pitfall).
    await session.commit()

    # ---- Transaction B: assistant message + counters ----
    async with session.begin():
        # Re-read the conversation row inside this txn so ORM state is
        # bound; ``conversation`` from txn A is now detached-ish.
        conversation = (
            await session.execute(
                select(Conversation).where(Conversation.id == conversation.id)
            )
        ).scalar_one()

        content: dict = {"type": "text", "text": assistant_text}
        if citations_payload:
            content["citations"] = citations_payload
        if related:
            content["image_matches"] = related
        if assistant_meta:
            content["meta"] = assistant_meta
        if assistant_placeholder:
            content["placeholder"] = True

        assistant_message = Message(
            conversation_id=conversation.id,
            organization_id=organization_id,
            role="assistant",
            content=content,
            author_user_id=None,
            tokens={
                "prompt": assistant_meta.get("prompt_tokens", 0) or 0,
                "completion": assistant_meta.get("completion_tokens", 0) or 0,
            },
            cost={"amount": 0, "currency": "USD"},
            correlation_id=correlation_id,
            status="completed" if not assistant_placeholder else "failed",
        )
        session.add(assistant_message)
        await session.flush()

        conversation.message_count = conversation.message_count + 2
        conversation.last_message_at = assistant_message.created_at
        if not conversation.title:
            conversation.title = text[:_TITLE_MAX]

        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=user_id,
                action="message.sent",
                resource_type="conversation",
                resource_id=conversation.id,
                space_id=conversation.space_id,
                request_id=correlation_id,
                payload={
                    "conversation_id": str(conversation.id),
                    "user_message_id": str(user_message.id),
                    "assistant_message_id": str(assistant_message.id),
                    "citation_count": len(citations_payload),
                    "model_id": assistant_meta.get("model_id"),
                    "retrieved_document_ids": retrieved_doc_ids,
                    "scope_mode": assistant_meta.get("scope_mode"),
                    "error": error_note,
                    **({"related_picture_ids": [m["document_id"] for m in related]} if related else {}),
                },
            )
        )
        await session.flush()
        await session.refresh(conversation, attribute_names=["updated_at"])

    return MessagePair(
        user_message=user_message,
        assistant_message=assistant_message,
        conversation=conversation,
    )


async def send_image_search(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    image: bytes | None,
    text: str | None,
    top_k: int = 12,
    filename: str | None = None,
) -> MessagePair:
    """Answer a picture and/or a description with similar pictures from the knowledge base.

    The search looks only in the spaces this conversation may search (the person's live
    permissions, narrowed to the spaces they picked), and every result is confirmed
    against the database. No model is called, so no tokens are spent.

    The uploaded picture is turned into a vector in memory. Once the search has succeeded it
    is also kept in object storage for its sender (see ``services.conversation_pictures``),
    so the thread shows it when they come back; the stored user message records that a
    picture was used, its id, and any words. Nothing is written, and nothing is kept, if the
    query cannot be read (a bad file, an empty query, search switched off): those raise
    before any message exists. If keeping the picture fails, the search still answers and
    the question is recorded as having no kept picture.
    """

    from quberi_api.services.rag import image_search

    words = (text or "").strip()

    # ---- Read phase: access, scope, query vector, search. Nothing is written yet. ----
    view = await get_conversation_for_user(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=user_id,
    )
    conversation = view.conversation
    scope, scope_mode, doc_access = await effective_access_for_conversation(
        session, conversation=conversation, caller_user_id=user_id
    )
    vector = await image_search.embed_query(image=image, text=words or None)
    qdrant = create_qdrant_client(get_settings())
    try:
        found = await image_search.search_images(
            session,
            qdrant=qdrant,
            organization_id=organization_id,
            space_ids=scope,
            query_vector=vector,
            top_k=top_k,
            doc_access=doc_access,
        )
    finally:
        await qdrant.close()
    matches = [_picture_match_dict(m) for m in found.matches]
    # The reads above began a transaction; end it before the write transaction.
    await session.commit()

    # Keep the picture only now that the search has worked, so a refused query leaves nothing.
    kept = None
    if image is not None:
        kept = await conversation_pictures.put(
            organization_id=organization_id,
            conversation_id=conversation_id,
            data=image,
            filename=filename,
        )
    used: dict = {"picture": image is not None, "description": bool(words)}
    if kept is not None:
        used["stored"] = True
        used["picture_id"] = str(kept.picture_id)
    reply = (
        f"找到 {len(matches)} 张相似的图片。"
        if matches
        else "没有找到相似的图片。图片需要先上传到你有权限的空间，并等待索引完成。"
    )
    try:
        return await _persist_image_turn(
            session,
            conversation_id=conversation_id,
            organization_id=organization_id,
            user_id=user_id,
            user_content={"type": "text", "text": words, "image_query": used},
            title=words[:_TITLE_MAX] if words else "以图搜图",
            reply=reply,
            matches=matches,
            kind="image_search",
            scope_mode=scope_mode,
            scope_space_ids=found.space_ids,
            audit_query=used,
            picture=kept,
        )
    except BaseException:
        # The question was not written, so the object has no owner: take it back.
        if kept is not None:
            await conversation_pictures.discard(kept.storage_key)
        raise


async def _persist_image_turn(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    user_content: dict,
    title: str,
    reply: str,
    matches: list[dict],
    kind: str,
    scope_mode: str,
    scope_space_ids: list[uuid.UUID],
    audit_query: dict,
    correlation_id: str | None = None,
    picture: conversation_pictures.StoredPicture | None = None,
) -> MessagePair:
    """Write a picture turn: both messages, the counters and the audit event together.

    No model is called, so the assistant message costs nothing. The caller must have
    ended its read transaction. Only ids are audited, never the words, a file name or a
    picture. ``picture`` (already in object storage) gets its row in the same transaction.
    """

    correlation_id = correlation_id or uuid.uuid4().hex
    # Both rows are written in one transaction, so the database clock would give them the
    # same time and the list (oldest first, then by id) could show the answer above the
    # question. Stamp the question first, explicitly.
    asked_at = datetime.now(timezone.utc)
    async with session.begin():
        conversation = (
            await session.execute(select(Conversation).where(Conversation.id == conversation_id))
        ).scalar_one()
        user_message = Message(
            conversation_id=conversation.id,
            organization_id=organization_id,
            role="user",
            content=user_content,
            author_user_id=user_id,
            tokens={},
            cost={},
            correlation_id=correlation_id,
            status="completed",
            created_at=asked_at,
        )
        session.add(user_message)
        await session.flush()
        if picture is not None:
            session.add(
                conversation_pictures.row_for(
                    picture,
                    organization_id=organization_id,
                    conversation_id=conversation.id,
                    message_id=user_message.id,
                    owner_user_id=user_id,
                )
            )
            await session.flush()
        assistant_message = Message(
            conversation_id=conversation.id,
            organization_id=organization_id,
            role="assistant",
            content={
                "type": "text",
                "text": reply,
                "image_matches": matches,
                "meta": {
                    "kind": kind,
                    "scope_mode": scope_mode,
                    "scope_space_ids": [str(sid) for sid in scope_space_ids],
                    "retrieved_count": len(matches),
                },
            },
            author_user_id=None,
            tokens={},
            cost={"amount": 0, "currency": "USD"},
            correlation_id=correlation_id,
            status="completed",
            created_at=asked_at + timedelta(milliseconds=1),
        )
        session.add(assistant_message)
        await session.flush()

        conversation.message_count = conversation.message_count + 2
        conversation.last_message_at = assistant_message.created_at
        if not conversation.title:
            conversation.title = title
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=user_id,
                action="message.sent",
                resource_type="conversation",
                resource_id=conversation.id,
                space_id=conversation.space_id,
                request_id=correlation_id,
                payload={
                    "kind": kind,
                    "conversation_id": str(conversation.id),
                    "user_message_id": str(user_message.id),
                    "assistant_message_id": str(assistant_message.id),
                    "citation_count": 0,
                    "model_id": None,
                    "retrieved_document_ids": [m["document_id"] for m in matches],
                    "query": audit_query,
                    "scope_mode": scope_mode,
                    "error": None,
                },
            )
        )
        await session.flush()
        await session.refresh(user_message, attribute_names=["created_at"])
        await session.refresh(assistant_message, attribute_names=["created_at"])
        await session.refresh(conversation, attribute_names=["updated_at"])

    return MessagePair(
        user_message=user_message,
        assistant_message=assistant_message,
        conversation=conversation,
    )


# ---------------------------------------------------------------------------
# Picture questions typed into the ordinary chat box
# ---------------------------------------------------------------------------

# Below this best score the closest pictures are probably not what was asked for.
# Chinese descriptions score higher than English ones even for unrelated pictures, so
# this is a hint to hedge the wording, never a filter.
WEAK_PICTURE_MATCH = 0.10
_PICTURE_CHAT_TOP_K = 6
_PICTURE_LIST_LIMIT = 30


@dataclass(frozen=True)
class PicturePlan:
    """What a picture question will be answered with. Built before anything is written."""

    text: str
    kind: str  # "image_list" | "image_search"
    reply: str
    matches: list[dict]
    scope_mode: str
    scope_space_ids: list[uuid.UUID]


def _picture_reply(*, kind: str, shown: int, total: int, best: float | None) -> str:
    if kind == "image_list":
        if total > shown:
            return f"知识库里共有 {total} 张图片，先列出最新的 {shown} 张。"
        return f"知识库里共有 {total} 张图片。"
    if best is not None and best < WEAK_PICTURE_MATCH:
        return f"没有明显吻合的图片，以下是最接近的 {shown} 张，请自行判断是否相关。"
    return f"找到 {shown} 张相似的图片。"


def _picture_match_dict(m) -> dict:
    """One found picture as stored in a message's ``image_matches``."""

    return {
        "document_id": str(m.document_id),
        "document_name": m.document_name,
        "space_id": str(m.space_id),
        "space_name": m.space_name,
        "mime_type": m.mime_type,
        "size_bytes": m.size_bytes,
        "score": round(m.score, 4),
    }


def _readable_pictures(organization_id: uuid.UUID, scope, doc_access) -> tuple:
    """WHERE clauses for the pictures this person may see right now (live permissions)."""

    from quberi_api.domain.documents.models import Document
    from quberi_api.services.documents.core import file_type_clause

    return (
        Document.organization_id == organization_id,
        or_(Document.space_id.in_(scope), Document.id.in_(set(doc_access.granted))),
        Document.id.not_in(doc_access.denied),
        Document.status != "archived",
        Document.review_state == "approved",
        file_type_clause(Document.mime_type, "image"),
    )


# Pictures that may go with an ordinary answer. A few at most, and only when the question is
# short enough to be a name or a phrase rather than a paragraph (the picture model reads
# 64 tokens and a long question matches nothing in particular).
RELATED_PICTURE_TOP_K = 4
RELATED_PICTURE_MIN_SCORE = WEAK_PICTURE_MATCH
RELATED_PICTURE_MAX_CHARS = 40


async def related_pictures(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    text: str,
) -> list[dict]:
    """Pictures that might be what an ordinary question was after, or ``[]``.

    Every question is also looked up in the picture index, because a text model cannot read
    pictures and would answer "nothing found" about ones that exist ("动物母狮"). Only the
    person's live permissions decide what can appear (the same filter as picture search), and
    the closest few are kept only if they score at least ``RELATED_PICTURE_MIN_SCORE``.

    The score cannot tell a right picture from the nearest wrong one (unrelated words reach
    the same numbers), so these are offered as "possibly related", beside the answer, never
    in place of it. Anything that goes wrong returns ``[]``: this must never break an answer.
    Ends with the session's read transaction still open; callers commit before writing. A
    failure undoes only its own SAVEPOINT, so the caller's objects stay usable.
    """

    from quberi_api.domain.documents.models import Document
    from quberi_api.services.rag import image_search

    query = (text or "").strip()
    settings = get_settings()
    if not query or len(query) > RELATED_PICTURE_MAX_CHARS or not settings.image_search_enabled:
        return []
    try:
        # A SAVEPOINT: if anything inside fails, only it is undone. A plain rollback would
        # expire every object the caller holds (the conversation) and break the answer.
        async with session.begin_nested():
            view = await get_conversation_for_user(
                session,
                conversation_id=conversation_id,
                organization_id=organization_id,
                user_id=user_id,
            )
            conversation = view.conversation
            if _agent_binding(conversation) is not None:
                return []  # an agent decides for itself what to look up
            scope, _mode, doc_access = await effective_access_for_conversation(
                session, conversation=conversation, caller_user_id=user_id
            )
            if not scope and not doc_access.granted:
                return []
            any_picture = (
                await session.execute(
                    select(Document.id)
                    .where(*_readable_pictures(organization_id, scope, doc_access))
                    .limit(1)
                )
            ).first()
            if any_picture is None:
                return []  # nothing to find: do not even load the picture model
            vector = await image_search.embed_query(image=None, text=query)
            qdrant = create_qdrant_client(settings)
            try:
                found = await image_search.search_images(
                    session,
                    qdrant=qdrant,
                    organization_id=organization_id,
                    space_ids=scope,
                    query_vector=vector,
                    top_k=RELATED_PICTURE_TOP_K,
                    doc_access=doc_access,
                )
            finally:
                await qdrant.close()
    except Exception:  # noqa: BLE001 - a side search must never break the answer
        import logging as _logging

        _logging.getLogger(__name__).exception("related pictures failed; answering without them")
        return []
    return [_picture_match_dict(m) for m in found.matches if m.score >= RELATED_PICTURE_MIN_SCORE]


async def plan_picture_turn(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    text: str,
) -> PicturePlan | None:
    """Decide whether a typed message is a picture question and, if so, answer it.

    Returns ``None`` (the message then goes down the normal text path) unless the text
    clearly asks for pictures, the conversation is not bound to an agent, picture search
    is on, and the person can read at least one picture. Looks only where the
    conversation may search, with the person's live permissions. Writes nothing and ends
    its own read transaction, so callers can skip the model quota for a plan.
    """

    from quberi_api.domain.documents.models import Document
    from quberi_api.services.rag import image_search, picture_intent

    intent = picture_intent.classify(text)
    if intent is None or not get_settings().image_search_enabled:
        return None
    view = await get_conversation_for_user(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=user_id,
    )
    conversation = view.conversation
    if _agent_binding(conversation) is not None:
        await session.rollback()
        return None
    scope, scope_mode, doc_access = await effective_access_for_conversation(
        session, conversation=conversation, caller_user_id=user_id
    )
    if not scope and not doc_access.granted:
        await session.rollback()
        return None

    readable = _readable_pictures(organization_id, scope, doc_access)
    total = (
        await session.execute(select(func.count()).select_from(Document).where(*readable))
    ).scalar_one()
    if total == 0:
        await session.rollback()
        return None  # no pictures to show: let the text path answer

    matches: list[dict] = []
    best: float | None = None
    if intent.kind == "list":
        rows = (
            await session.execute(
                select(Document, Space.name)
                .join(Space, Space.id == Document.space_id)
                .where(*readable, Space.status == "active", Space.organization_id == organization_id)
                .order_by(Document.created_at.desc())
                .limit(_PICTURE_LIST_LIMIT)
            )
        ).all()
        matches = [
            {
                "document_id": str(d.id),
                "document_name": d.name,
                "space_id": str(d.space_id),
                "space_name": space_name,
                "mime_type": d.mime_type,
                "size_bytes": int(d.size_bytes or 0),
            }
            for d, space_name in rows
        ]
        kind = "image_list"
    else:
        kind = "image_search"
        try:
            vector = await image_search.embed_query(image=None, text=intent.subject)
            qdrant = create_qdrant_client(get_settings())
            try:
                found = await image_search.search_images(
                    session,
                    qdrant=qdrant,
                    organization_id=organization_id,
                    space_ids=scope,
                    query_vector=vector,
                    top_k=_PICTURE_CHAT_TOP_K,
                    doc_access=doc_access,
                )
            finally:
                await qdrant.close()
        except Exception:  # noqa: BLE001 - never let the picture shortcut break chat
            import logging as _logging

            _logging.getLogger(__name__).exception("picture question failed; using text path")
            await session.rollback()
            return None
        matches = [_picture_match_dict(m) for m in found.matches]
        best = max((m["score"] for m in matches), default=None)
        if not matches:
            await session.rollback()
            return None  # pictures exist but none is indexed yet: let the text path answer
        if intent.soft and (best is None or best < WEAK_PICTURE_MATCH):
            # "有风景图吗" may have meant a chart or diagram in a document. Only answer with
            # pictures when one really matches; otherwise the text path answers as before.
            await session.rollback()
            return None
    await session.commit()
    return PicturePlan(
        text=text.strip(),
        kind=kind,
        reply=_picture_reply(kind=kind, shown=len(matches), total=total, best=best),
        matches=matches,
        scope_mode=scope_mode,
        scope_space_ids=list(scope),
    )


async def _answer_picture_plan(
    session: AsyncSession,
    *,
    plan: PicturePlan,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> MessagePair:
    return await _persist_image_turn(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=user_id,
        user_content={"type": "text", "text": plan.text},
        title=plan.text[:_TITLE_MAX],
        reply=plan.reply,
        matches=plan.matches,
        kind=plan.kind,
        scope_mode=plan.scope_mode,
        scope_space_ids=plan.scope_space_ids,
        audit_query={"picture": False, "description": plan.kind == "image_search"},
    )


async def send_message_stream(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    identity_id: uuid.UUID | None = None,
    text: str,
    review: bool = False,
    picture_plan: PicturePlan | None = None,
    with_pictures: bool = True,
):
    """Streaming twin of :func:`send_message`.

    Async generator yielding ``(kind, data)`` tuples ready for SSE
    serialisation. The last event has ``kind='done'`` and carries the
    fully persisted assistant message. Sequence::

        ('user_message', {…})        # persisted user turn
        ('retrieved',    {citations})
        ('delta', {text: '...'})    # 0..N
        ('assistant_message', {…})   # persisted assistant turn (final)
        ('done', {})

    Callers must consume to completion (the assistant message is only
    persisted after the LLM finishes) — cancelling mid-stream leaves
    the user message committed but no assistant reply, which the UI
    can render as "generation aborted".

    With ``review=True`` the turn stops before the model call: after
    ``user_message`` it emits ``review_required`` (the assembled prompt, with
    suspected sensitive data marked) and ends. The person then continues with
    :func:`resume_reviewed_stream` or :func:`cancel_review`. When no model call
    would happen anyway (nothing matched), the turn simply proceeds.
    """

    text = text.strip() if isinstance(text, str) else ""
    if not text:
        raise InvalidInputError("message text is required")
    if len(text) > _MAX_MESSAGE_TEXT:
        raise InvalidInputError(
            f"message text exceeds {_MAX_MESSAGE_TEXT} character limit"
        )

    if picture_plan is not None:
        # A picture question: no model, no review hold, no quota. Same event order
        # as a text turn, minus ``retrieved`` and ``delta``.
        pair = await _answer_picture_plan(
            session,
            plan=picture_plan,
            conversation_id=conversation_id,
            organization_id=organization_id,
            user_id=user_id,
        )
        um, am = pair.user_message, pair.assistant_message
        yield "user_message", {
            "id": str(um.id),
            "conversation_id": str(conversation_id),
            "role": "user",
            "content": um.content,
            "author_user_id": str(um.author_user_id) if um.author_user_id else None,
            "status": um.status,
            "correlation_id": um.correlation_id,
            "created_at": um.created_at.isoformat(),
        }
        yield "assistant_message", _assistant_event(
            am, am.content, conversation_id, am.correlation_id
        )
        yield "done", {}
        return

    correlation_id = uuid.uuid4().hex

    # ---- Txn A: user message ----
    async with session.begin():
        view = await get_conversation_for_user(
            session,
            conversation_id=conversation_id,
            organization_id=organization_id,
            user_id=user_id,
        )
        conversation = view.conversation
        if review and _agent_binding(conversation) is not None:
            # An agent calls the model many times in a loop; checking each call
            # is not supported yet, and silently skipping the check would
            # mislead. Refuse (this also rolls back the user message).
            raise InvalidInputError(
                "智能体对话暂不支持发送前检查，请先关闭「发送前检查」。 "
                "Agent conversations do not support check-before-sending yet; "
                "turn it off first."
            )
        user_message = Message(
            conversation_id=conversation.id,
            organization_id=organization_id,
            role="user",
            content={"type": "text", "text": text},
            author_user_id=user_id,
            tokens={},
            cost={},
            correlation_id=correlation_id,
            status="completed",
        )
        session.add(user_message)
        await session.flush()
        await session.refresh(user_message, attribute_names=["created_at"])

    yield "user_message", {
        "id": str(user_message.id),
        "conversation_id": str(conversation.id),
        "role": "user",
        "content": user_message.content,
        "author_user_id": str(user_message.author_user_id)
        if user_message.author_user_id
        else None,
        "status": user_message.status,
        "correlation_id": correlation_id,
        "created_at": user_message.created_at.isoformat(),
    }

    history_msgs = await _recent_history_for_llm(
        session,
        conversation_id=conversation.id,
        exclude_message_id=user_message.id,
    )

    # ---- Generation: agent tool-loop when bound, else plain RAG ----
    agent_id = _agent_binding(conversation)
    accumulated_answer = ""
    citations_payload: list[dict] = []
    assistant_meta: dict = {}
    error_note: str | None = None
    scope_mode_used = "auto"
    scope_space_ids: list[str] = []
    retrieved_count = 0
    # Ids of every document retrieval handed to the model (audit only).
    retrieved_doc_ids: list[str] = []

    if agent_id is not None:
        # Agent branch: builtin runs the in-house tool loop; external
        # forwards to a third-party endpoint. Both stream (kind, data).
        from quberi_api.services.agents import config as agents_config
        from quberi_api.services.agents.external import run_external_agent_stream
        from quberi_api.services.agents.runner import run_agent_stream

        agent_trace: list[dict] = []
        try:
            agent_obj = await agents_config.get_agent(
                session, organization_id=organization_id, agent_id=agent_id
            )
            # Release the read txn before the network-bound stream.
            await session.commit()
            if agent_obj.kind == "external":
                agent_stream = run_external_agent_stream(
                    session,
                    agent=agent_obj,
                    user_id=user_id,
                    query=text,
                    history=history_msgs,
                )
            else:
                agent_stream = run_agent_stream(
                    session,
                    organization_id=organization_id,
                    user_id=user_id,
                    query=text,
                    agent_id=agent_id,
                    history=history_msgs,
                )
            async for kind, data in agent_stream:
                if kind == "delta":
                    delta_text = data.get("text", "")
                    accumulated_answer += delta_text
                    yield "delta", {"text": delta_text}
                elif kind in ("tool_call", "tool_result", "note"):
                    step = {"kind": kind, **data}
                    agent_trace.append(step)
                    if kind == "tool_result":
                        retrieved_doc_ids = document_ids_from_trace(agent_trace)
                    yield "agent_step", step
                elif kind == "error":
                    error_note = data.get("message", "unknown error")
                    yield "error", data
                elif kind == "done":
                    assistant_meta = {
                        "model_id": data.get("model_id"),
                        "agent_id": str(agent_id),
                        "iterations": data.get("iterations"),
                        "tool_call_count": data.get("tool_call_count"),
                        "trace": agent_trace,
                    }
        except Exception as exc:  # noqa: BLE001
            import logging as _logging

            _logging.getLogger(__name__).exception(
                "agent stream failed for conversation %s", conversation.id
            )
            error_note = f"{type(exc).__name__}: {exc}"
            accumulated_answer = (
                accumulated_answer
                or "智能体运行时发生错误：稍后再试，或让管理员检查智能体配置与模型设置。"
            )
            if not assistant_meta:
                assistant_meta = {"error": error_note, "agent_id": str(agent_id)}
            yield "error", {"message": error_note}
    else:
        qdrant = create_qdrant_client(get_settings())
        try:
            retrieved = await retrieve_for_conversation(
                session,
                qdrant=qdrant,
                conversation=conversation,
                caller_user_id=user_id,
                query=text,
            )
            scope_mode_used = retrieved.scope_mode
            scope_space_ids = [str(s) for s in retrieved.space_ids]
            retrieved_count = len(retrieved.chunks)
            retrieved_doc_ids = document_ids_from_chunks(retrieved.chunks)

            ov_model, ov_binding = await _effective_override(
                session,
                conversation=conversation,
                organization_id=organization_id,
                user_id=user_id,
            )
            if review:
                history_sent = history_msgs[-8:]  # all the prompt carries
                scope_empty = len(retrieved.space_ids) == 0
                if (
                    outgoing_messages(
                        query=text,
                        chunks=retrieved.chunks,
                        history=history_sent,
                        scope_is_empty=scope_empty,
                    )
                    is not None
                ):
                    pending = send_review.PendingReview(
                        id=send_review.new_pending_id(),
                        organization_id=str(organization_id),
                        user_id=str(user_id),
                        conversation_id=str(conversation.id),
                        user_message_id=str(user_message.id),
                        correlation_id=correlation_id,
                        query=text,
                        history=[{"role": m.role, "content": m.content} for m in history_sent],
                        chunks=[send_review.chunk_to_dict(c) for c in retrieved.chunks],
                        scope_is_empty=scope_empty,
                        scope_mode=retrieved.scope_mode,
                        scope_space_ids=[str(x) for x in retrieved.space_ids],
                        with_pictures=with_pictures,
                        model_id=ov_model,
                        binding_id=str(ov_binding) if ov_binding else None,
                        model=await describe_chat_model(
                            session,
                            organization_id=organization_id,
                            identity_id=identity_id,
                            model_id=ov_model,
                            binding_id=ov_binding,
                        ),
                    )
                    redis = create_redis_client(get_settings())
                    try:
                        await send_review.save(redis, pending)
                    finally:
                        await redis.aclose()
                    yield "review_required", send_review.build_payload(pending)
                    yield "done", {}
                    return
            async for event in generate_answer_stream(
                session,
                organization_id=organization_id,
                identity_id=identity_id,
                query=text,
                chunks=retrieved.chunks,
                history=history_msgs,
                model_id=ov_model,
                binding_id=ov_binding,
                scope_is_empty=len(retrieved.space_ids) == 0,
            ):
                if event.kind == "retrieved":
                    # Cache these locally for the final assistant.content.
                    citations_payload = event.data.get("citations", [])
                    yield "retrieved", event.data
                elif event.kind == "delta":
                    delta_text = event.data.get("text", "")
                    accumulated_answer += delta_text
                    yield "delta", event.data
                elif event.kind == "error":
                    error_note = event.data.get("message", "unknown error")
                    yield "error", event.data
                elif event.kind == "done":
                    # Prefer the finaliser's authoritative payload.
                    accumulated_answer = event.data.get("answer", accumulated_answer)
                    citations_payload = event.data.get("citations", citations_payload)
                    assistant_meta = {
                        "model_id": event.data.get("model_id"),
                        "prompt_tokens": event.data.get("prompt_tokens", 0),
                        "completion_tokens": event.data.get("completion_tokens", 0),
                        "scope_mode": scope_mode_used,
                        "scope_space_ids": scope_space_ids,
                        "retrieved_count": retrieved_count,
                        "system_note": event.data.get("system_note", ""),
                    }
        except Exception as exc:  # noqa: BLE001
            import logging as _logging

            _logging.getLogger(__name__).exception(
                "RAG stream failed for conversation %s", conversation.id
            )
            error_note = f"{type(exc).__name__}: {exc}"
            accumulated_answer = (
                accumulated_answer
                or "生成回答时发生错误：稍后再试，或让管理员在\"AI 供应商\"设置里检查密钥与模型选择。"
            )
            assistant_meta = {"error": error_note}
            yield "error", {"message": error_note}
        finally:
            await qdrant.close()

    # Pictures that might be what was asked for (the model cannot read pictures).
    related = (
        []
        if not with_pictures or agent_id is not None or error_note
        else await related_pictures(
            session,
            conversation_id=conversation.id,
            organization_id=organization_id,
            user_id=user_id,
            text=text,
        )
    )

    # ---- Txn B: assistant message + counters ----
    assistant_message, content = await _persist_assistant_turn(
        session,
        conversation_id=conversation.id,
        organization_id=organization_id,
        user_id=user_id,
        user_message_id=user_message.id,
        correlation_id=correlation_id,
        user_text=text,
        accumulated_answer=accumulated_answer,
        citations_payload=citations_payload,
        assistant_meta=assistant_meta,
        error_note=error_note,
        related=related,
        audit_extra={
            "scope_mode": scope_mode_used,
            "retrieved_document_ids": retrieved_doc_ids,
            "streamed": True,
        },
    )
    yield "assistant_message", _assistant_event(
        assistant_message, content, conversation.id, correlation_id
    )
    yield "done", {}


def _assistant_event(
    assistant_message: Message,
    content: dict,
    conversation_id: uuid.UUID,
    correlation_id: str,
) -> dict:
    return {
        "id": str(assistant_message.id),
        "conversation_id": str(conversation_id),
        "role": "assistant",
        "content": content,
        "author_user_id": None,
        "status": assistant_message.status,
        "correlation_id": correlation_id,
        "created_at": assistant_message.created_at.isoformat(),
    }


async def _persist_assistant_turn(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    user_message_id: uuid.UUID,
    correlation_id: str,
    user_text: str,
    accumulated_answer: str,
    citations_payload: list[dict],
    assistant_meta: dict,
    error_note: str | None,
    audit_extra: dict,
    audit_action: str = "message.sent",
    related: list[dict] | None = None,
) -> tuple[Message, dict]:
    """Save the assistant's reply, bump the counters, write the audit event."""

    await session.commit()
    async with session.begin():
        conversation = (
            await session.execute(
                select(Conversation).where(Conversation.id == conversation_id)
            )
        ).scalar_one()
        content: dict = {"type": "text", "text": accumulated_answer}
        if citations_payload:
            content["citations"] = citations_payload
        if related:
            content["image_matches"] = related
        if assistant_meta:
            content["meta"] = assistant_meta
        if error_note:
            content["placeholder"] = True
        assistant_message = Message(
            conversation_id=conversation.id,
            organization_id=organization_id,
            role="assistant",
            content=content,
            author_user_id=None,
            tokens={
                "prompt": assistant_meta.get("prompt_tokens", 0) or 0,
                "completion": assistant_meta.get("completion_tokens", 0) or 0,
            },
            cost={"amount": 0, "currency": "USD"},
            correlation_id=correlation_id,
            status="failed" if error_note else "completed",
        )
        session.add(assistant_message)
        await session.flush()
        await session.refresh(assistant_message, attribute_names=["created_at"])
        conversation.message_count = conversation.message_count + 2
        conversation.last_message_at = assistant_message.created_at
        if not conversation.title:
            conversation.title = user_text[:_TITLE_MAX]
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=user_id,
                action=audit_action,
                resource_type="conversation",
                resource_id=conversation.id,
                space_id=conversation.space_id,
                request_id=correlation_id,
                payload={
                    "conversation_id": str(conversation.id),
                    "user_message_id": str(user_message_id),
                    "assistant_message_id": str(assistant_message.id),
                    "citation_count": len(citations_payload),
                    "model_id": assistant_meta.get("model_id"),
                    "error": error_note,
                    **({"related_picture_ids": [m["document_id"] for m in related]} if related else {}),
                    **audit_extra,
                },
            )
        )
    return assistant_message, content


# ---------------------------------------------------------------------------
# Review before sending (continuing, previewing or cancelling a held prompt)
# ---------------------------------------------------------------------------


async def _review_access(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """The person must still be allowed in the conversation (access can change)."""

    async with session.begin():
        await get_conversation_for_user(
            session,
            conversation_id=conversation_id,
            organization_id=organization_id,
            user_id=user_id,
        )


async def preview_review(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    pending_id: str,
    edits: send_review.ReviewEdits,
) -> dict:
    """The review card for the person's current edits; nothing is sent or used up."""

    await _review_access(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=user_id,
    )
    redis = create_redis_client(get_settings())
    try:
        pending = await send_review.peek(
            redis,
            pending_id=pending_id,
            organization_id=organization_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
    finally:
        await redis.aclose()
    return send_review.build_payload(pending, edits)


async def check_review_edits(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    pending_id: str,
    edits: send_review.ReviewEdits,
) -> None:
    """Refuse a bad approval up front (404 expired / 422 invalid edits).

    The reply is streamed, and by then the HTTP status is already 200, so the
    checks that can fail are run here first. Nothing is used up.
    """

    await preview_review(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=user_id,
        pending_id=pending_id,
        edits=edits,
    )


async def cancel_review(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    pending_id: str,
) -> dict:
    """Drop a held prompt without calling the model; leave a note in the chat."""

    await _review_access(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=user_id,
    )
    redis = create_redis_client(get_settings())
    try:
        pending = await send_review.take(
            redis,
            pending_id=pending_id,
            organization_id=organization_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
    finally:
        await redis.aclose()
    assistant_message, content = await _persist_assistant_turn(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=user_id,
        user_message_id=uuid.UUID(pending.user_message_id),
        correlation_id=pending.correlation_id,
        user_text=pending.query,
        accumulated_answer=(
            "已取消：这次提问没有发送给模型。 "
            "Cancelled: this question was not sent to the model."
        ),
        citations_payload=[],
        assistant_meta={"review": {"cancelled": True}},
        error_note=None,
        audit_extra={"llm_review": {"outcome": "cancelled"}},
        audit_action="message.send_cancelled",
    )
    return _assistant_event(
        assistant_message, content, conversation_id, pending.correlation_id
    )


async def resume_reviewed_stream(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    identity_id: uuid.UUID | None,
    pending_id: str,
    edits: send_review.ReviewEdits,
):
    """Call the model with the person's reviewed version of a held prompt.

    Yields the same events as the normal stream after the user turn:
    ``retrieved``, ``delta``..., ``assistant_message``, ``done``. The held
    prompt is used up, so it can be sent once only.
    """

    await _review_access(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=user_id,
    )
    ids = {
        "pending_id": pending_id,
        "organization_id": organization_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
    }
    redis = create_redis_client(get_settings())
    try:
        # Check the edits first, so a mistake can be fixed without losing the
        # held prompt; only then use it up.
        pending = await send_review.peek(redis, **ids)
        resolved = send_review.resolve(pending, edits)
        pending = await send_review.take(redis, **ids)
    finally:
        await redis.aclose()

    review_meta = {
        "edited_parts": resolved.edited_parts,
        "excluded_references": resolved.excluded_references,
    }
    included_doc_ids = document_ids_from_chunks(resolved.chunks)
    all_doc_ids = document_ids_from_chunks(
        [send_review.chunk_from_dict(c) for c in pending.chunks]
    )
    excluded_doc_ids = [d for d in all_doc_ids if d not in set(included_doc_ids)]

    accumulated_answer = ""
    citations_payload: list[dict] = []
    assistant_meta: dict = {}
    error_note: str | None = None
    try:
        async for event in generate_answer_stream(
            session,
            organization_id=organization_id,
            identity_id=identity_id,
            query=resolved.query,
            chunks=resolved.chunks,
            history=resolved.history,
            model_id=pending.model_id,
            binding_id=uuid.UUID(pending.binding_id) if pending.binding_id else None,
            scope_is_empty=pending.scope_is_empty,
            text_overrides=resolved.text_overrides,
        ):
            if event.kind == "retrieved":
                citations_payload = event.data.get("citations", [])
                yield "retrieved", event.data
            elif event.kind == "delta":
                accumulated_answer += event.data.get("text", "")
                yield "delta", event.data
            elif event.kind == "error":
                error_note = event.data.get("message", "unknown error")
                yield "error", event.data
            elif event.kind == "done":
                accumulated_answer = event.data.get("answer", accumulated_answer)
                citations_payload = event.data.get("citations", citations_payload)
                assistant_meta = {
                    "model_id": event.data.get("model_id"),
                    "prompt_tokens": event.data.get("prompt_tokens", 0),
                    "completion_tokens": event.data.get("completion_tokens", 0),
                    "scope_mode": pending.scope_mode,
                    "scope_space_ids": pending.scope_space_ids,
                    "retrieved_count": len(resolved.chunks),
                    "system_note": event.data.get("system_note", ""),
                    "review": review_meta,
                }
    except Exception as exc:  # noqa: BLE001
        import logging as _logging

        _logging.getLogger(__name__).exception(
            "reviewed send failed for conversation %s", conversation_id
        )
        error_note = f"{type(exc).__name__}: {exc}"
        accumulated_answer = (
            accumulated_answer
            or "生成回答时发生错误：稍后再试，或让管理员在\"AI 供应商\"设置里检查密钥与模型选择。"
        )
        assistant_meta = {"error": error_note, "review": review_meta}
        yield "error", {"message": error_note}

    # Looking pictures up sends nothing to the model, so a held prompt's edits do not apply:
    # it is the question as asked.
    related = (
        []
        if error_note or not pending.with_pictures
        else await related_pictures(
            session,
            conversation_id=conversation_id,
            organization_id=organization_id,
            user_id=user_id,
            text=pending.query,
        )
    )
    assistant_message, content = await _persist_assistant_turn(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=user_id,
        user_message_id=uuid.UUID(pending.user_message_id),
        correlation_id=pending.correlation_id,
        user_text=pending.query,
        accumulated_answer=accumulated_answer,
        citations_payload=citations_payload,
        assistant_meta=assistant_meta,
        error_note=error_note,
        related=related,
        audit_extra={
            "scope_mode": pending.scope_mode,
            # What the model could actually see: the passages that were kept.
            "retrieved_document_ids": included_doc_ids,
            "streamed": True,
            "llm_review": {
                "outcome": "approved",
                "edited_parts": resolved.edited_parts,
                "excluded_references": resolved.excluded_references,
                "excluded_document_ids": excluded_doc_ids,
            },
        },
    )
    yield "assistant_message", _assistant_event(
        assistant_message, content, conversation_id, pending.correlation_id
    )
    yield "done", {}


async def _recent_history_for_llm(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    exclude_message_id: uuid.UUID,
    limit: int = 8,
) -> list[LlmMessage]:
    """Return the last ``limit`` messages (excluding the current user turn).

    Only user/assistant text is included; tool + system messages are
    generated fresh in the prompt assembler.
    """

    rows = (
        await session.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.id != exclude_message_id,
                Message.role.in_(("user", "assistant")),
                Message.status != "failed",
            )
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    ordered = list(reversed(rows))
    result: list[LlmMessage] = []
    for row in ordered:
        text = _extract_text(row.content or {})
        if not text:
            continue
        result.append(LlmMessage(role=row.role, content=text))
    return result


# ---------------------------------------------------------------------------
# Knowledge scope (Slice F)
# ---------------------------------------------------------------------------

_SCOPE_MODES: frozenset[str] = frozenset(("auto", "custom"))


@dataclass(frozen=True)
class KnowledgeSourceView:
    """A single space linked to a conversation as an extra RAG source."""

    link: ConversationKnowledgeSource
    space_id: uuid.UUID
    space_name: str
    space_slug: str


async def _load_conversation_for_user(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Conversation:
    """Fetch a conversation and confirm the caller can see its anchor space."""

    row = (
        await session.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"conversation not found: {conversation_id}")
    # The visibility check for the anchor space also serves as tenant scoping.
    await spaces_service.get_space_for_user(
        session,
        space_id=row.space_id,
        organization_id=organization_id,
        user_id=user_id,
    )
    return row


async def _caller_has_space_view(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
) -> bool:
    """``True`` iff the user has viewer/editor/admin/owner on the space, or at least
    one document of it is shared with them individually.

    Personal spaces owned by the user always count, matching the rest of
    the platform.
    """

    space = (
        await session.execute(
            select(Space).where(
                Space.id == space_id,
                Space.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if space is None:
        return False
    if space.owner_type == "user" and space.owner_id == user_id:
        return True
    codes = await resolve_effective_role_codes(
        session,
        user_id=user_id,
        organization_id=organization_id,
        resource_type="space",
        resource_id=space_id,
    )
    if codes:
        return True
    # Not readable as a whole, but some of its documents may be shared with the person.
    access = await load_doc_access(
        session, user_id=user_id, organization_id=organization_id
    )
    return space_id in access.granted.values()


async def set_scope_mode(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    caller_user_id: uuid.UUID,
    mode: str,
) -> Conversation:
    """Flip a conversation between ``auto`` and ``custom`` scope."""

    if mode not in _SCOPE_MODES:
        raise InvalidInputError(
            f"unknown scope mode {mode!r} (allowed: {sorted(_SCOPE_MODES)})"
        )

    conv = await _load_conversation_for_user(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=caller_user_id,
    )
    if conv.scope_mode == mode:
        # Nothing to do — but don't fail; idempotent set is a friendlier API.
        return conv

    previous = conv.scope_mode

    # When entering ``custom`` from ``auto`` we seed the explicit source
    # list with every space the caller can currently reach, so the initial
    # custom selection matches what auto was already searching. The user
    # then unchecks whichever spaces (including the personal/anchor space)
    # they don't want. Leaving ``custom`` clears the rows again.
    seed_ids: list[uuid.UUID] = []
    if mode == "custom":
        accessible = await list_accessible_space_ids(
            session, user_id=caller_user_id, organization_id=organization_id
        )
        seed_ids = sorted(accessible, key=str)

    # ``_load_conversation_for_user`` auto-began a transaction via the
    # visibility SELECT; commit before opening a fresh one for the write.
    await session.commit()
    async with session.begin():
        conv.scope_mode = mode
        # Reset any stale rows, then (for custom) seed the full set.
        await session.execute(
            delete(ConversationKnowledgeSource).where(
                ConversationKnowledgeSource.conversation_id == conv.id
            )
        )
        for sid in seed_ids:
            session.add(
                ConversationKnowledgeSource(
                    conversation_id=conv.id,
                    space_id=sid,
                    added_by_user_id=caller_user_id,
                )
            )
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=caller_user_id,
                action="conversation.scope_mode_changed",
                resource_type="conversation",
                resource_id=conv.id,
                space_id=conv.space_id,
                payload={"from": previous, "to": mode},
            )
        )
        await session.flush()
        # ``updated_at`` is server-managed via the TimestampMixin trigger,
        # so we must eagerly refresh it here — otherwise the outer
        # serialiser blows up with a lazy-load-outside-session error when
        # Pydantic tries to read attributes on the detached row.
        await session.refresh(conv, attribute_names=["updated_at"])
    return conv


async def set_conversation_model(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    caller_user_id: uuid.UUID,
    caller_identity_id: uuid.UUID | None = None,
    model_id: str | None,
    binding_id: uuid.UUID | None,
) -> Conversation:
    """Set (or clear) the conversation's model override.

    ``model_id=None`` clears it (back to org default). A non-null choice is
    validated against the caller's usable + allowed models so a user can't
    pin a model they aren't permitted to use.
    """

    conv = await _load_conversation_for_user(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=caller_user_id,
    )

    clearing = not model_id or not binding_id
    if not clearing:
        from quberi_api.services import ai_providers as ai_providers_service

        usable = await ai_providers_service.list_usable_chat_models_for_user(
            session,
            organization_id=organization_id,
            user_id=caller_user_id,
            identity_id=caller_identity_id,
        )
        ok = any(
            u.model_id == model_id and str(u.binding_id) == str(binding_id)
            for u in usable
        )
        if not ok:
            raise InvalidInputError("model is not available to you")

    await session.commit()
    async with session.begin():
        attrs = dict(conv.attributes or {})
        if clearing:
            attrs.pop("model", None)
        else:
            attrs["model"] = {"model_id": model_id, "binding_id": str(binding_id)}
        conv.attributes = attrs
        await session.flush()
        await session.refresh(conv, attribute_names=["updated_at"])
    return conv


async def set_conversation_agent(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    caller_user_id: uuid.UUID,
    agent_id: uuid.UUID | None,
) -> Conversation:
    """Bind (or clear) the conversation's agent.

    ``agent_id=None`` clears the binding (back to plain RAG). A non-null
    agent must be usable by the caller (visibility check); binding it means
    subsequent messages run the agent's tool loop instead of plain RAG.
    """

    conv = await _load_conversation_for_user(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=caller_user_id,
    )

    clearing = agent_id is None
    if not clearing:
        from quberi_api.services.agents import config as agents_config

        agent = await agents_config.get_agent(
            session, organization_id=organization_id, agent_id=agent_id
        )
        usable = await agents_config.can_use_agent(
            session,
            organization_id=organization_id,
            user_id=caller_user_id,
            agent=agent,
        )
        if not usable:
            raise InvalidInputError("agent is not available to you")

    await session.commit()
    async with session.begin():
        attrs = dict(conv.attributes or {})
        if clearing:
            attrs.pop("agent", None)
        else:
            attrs["agent"] = {"agent_id": str(agent_id)}
        conv.attributes = attrs
        await session.flush()
        await session.refresh(conv, attribute_names=["updated_at"])
    return conv


async def rename_conversation(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    caller_user_id: uuid.UUID,
    title: str,
) -> Conversation:
    """Rename a conversation. Anyone who can see the thread may rename it."""

    normalised = (title or "").strip()
    if not normalised:
        raise InvalidInputError("title must not be empty")
    normalised = normalised[:_TITLE_MAX]

    conv = await _load_conversation_for_user(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=caller_user_id,
    )

    await session.commit()
    async with session.begin():
        conv.title = normalised
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=caller_user_id,
                action="conversation.renamed",
                resource_type="conversation",
                resource_id=conv.id,
                space_id=conv.space_id,
                payload={"title": normalised},
            )
        )
        await session.flush()
        await session.refresh(conv, attribute_names=["updated_at"])
    return conv


async def list_scope_sources(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    caller_user_id: uuid.UUID,
) -> list[KnowledgeSourceView]:
    """Extra source spaces attached to a conversation, oldest first.

    Always returns the *stored* links — including any that the current
    caller has since lost access to. The RAG pipeline is what filters
    against live access at query time; the management surface should
    still show the link so an admin can prune it.
    """

    await _load_conversation_for_user(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=caller_user_id,
    )
    rows = (
        await session.execute(
            select(
                ConversationKnowledgeSource,
                Space.id,
                Space.name,
                Space.slug,
            )
            .join(Space, Space.id == ConversationKnowledgeSource.space_id)
            .where(ConversationKnowledgeSource.conversation_id == conversation_id)
            .order_by(ConversationKnowledgeSource.created_at.asc())
        )
    ).all()
    return [
        KnowledgeSourceView(
            link=link, space_id=sid, space_name=name, space_slug=slug
        )
        for link, sid, name, slug in rows
    ]


async def add_scope_source(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    space_id: uuid.UUID,
    organization_id: uuid.UUID,
    caller_user_id: uuid.UUID,
) -> KnowledgeSourceView:
    """Attach an additional space as a knowledge source.

    The caller must have viewer+ on the target space *right now*. They
    also must be able to see the anchor conversation. We do NOT require
    ownership of the conversation — a colleague in the same anchor space
    can broaden the search for a shared thread.
    """

    conv = await _load_conversation_for_user(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=caller_user_id,
    )

    # The anchor/home space is NOT special anymore — it is just another
    # selectable source. It may be added or removed like any other space.

    can_see = await _caller_has_space_view(
        session,
        user_id=caller_user_id,
        organization_id=organization_id,
        space_id=space_id,
    )
    if not can_see:
        # NotFoundError (not permission) so we don't leak the existence
        # of spaces the caller can't see.
        raise NotFoundError(f"space not accessible: {space_id}")

    # Fetch space metadata for the return view.
    space = (
        await session.execute(
            select(Space).where(
                Space.id == space_id,
                Space.organization_id == organization_id,
            )
        )
    ).scalar_one()

    # Duplicate check via SELECT before insert — the unique index below
    # would also catch it, but this returns a clean error message.
    existing = (
        await session.execute(
            select(ConversationKnowledgeSource).where(
                ConversationKnowledgeSource.conversation_id == conversation_id,
                ConversationKnowledgeSource.space_id == space_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise AlreadyExistsError(
            f"space {space.slug!r} is already a knowledge source for this conversation"
        )

    # Same auto-txn housekeeping as ``set_scope_mode``.
    await session.commit()
    link = ConversationKnowledgeSource(
        conversation_id=conversation_id,
        space_id=space_id,
        added_by_user_id=caller_user_id,
    )
    async with session.begin():
        session.add(link)
        await session.flush()
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=caller_user_id,
                action="conversation.source_added",
                resource_type="conversation",
                resource_id=conversation_id,
                space_id=conv.space_id,
                payload={
                    "added_space_id": str(space_id),
                    "added_space_slug": space.slug,
                },
            )
        )
        await session.refresh(link, attribute_names=["created_at"])

    return KnowledgeSourceView(
        link=link,
        space_id=space.id,
        space_name=space.name,
        space_slug=space.slug,
    )


async def remove_scope_source(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    space_id: uuid.UUID,
    organization_id: uuid.UUID,
    caller_user_id: uuid.UUID,
) -> None:
    """Detach a knowledge source. Anyone who can see the conversation may."""

    conv = await _load_conversation_for_user(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=caller_user_id,
    )

    link = (
        await session.execute(
            select(ConversationKnowledgeSource).where(
                ConversationKnowledgeSource.conversation_id == conversation_id,
                ConversationKnowledgeSource.space_id == space_id,
            )
        )
    ).scalar_one_or_none()
    if link is None:
        raise NotFoundError(
            f"space {space_id} is not a knowledge source of this conversation"
        )

    await session.commit()
    async with session.begin():
        await session.delete(link)
        session.add(
            AuditEvent(
                organization_id=organization_id,
                actor_type="user",
                actor_id=caller_user_id,
                action="conversation.source_removed",
                resource_type="conversation",
                resource_id=conversation_id,
                space_id=conv.space_id,
                payload={"removed_space_id": str(space_id)},
            )
        )
