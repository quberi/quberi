"""Pydantic contracts for conversation & message endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: Optional[str] = Field(default=None, max_length=200)


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=8000)
    # Hold the prompt for the person to read and edit before the model is
    # called (streaming endpoint only).
    review: bool = False
    # Also look the question up in the picture index (and answer a plain "show me pictures"
    # from it). Off means text only: no picture lookup, no picture shortcut.
    pictures: bool = True


class ReviewReferenceEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1, le=100)
    include: bool = True
    # Exactly the text the person wants sent. Not stripped or normalised:
    # the server compares it with what it showed.
    text: Optional[str] = Field(default=None, max_length=1200)


class ReviewEditsRequest(BaseModel):
    """The person's version of a held prompt (see ``services.send_review``)."""

    model_config = ConfigDict(extra="forbid")

    question: Optional[str] = Field(default=None, max_length=8000)
    history: Optional[list[Optional[str]]] = Field(default=None, max_length=20)
    references: list[ReviewReferenceEdit] = Field(default_factory=list, max_length=100)


class SetConversationModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Both null → clear the override (fall back to org default).
    model_id: Optional[str] = Field(default=None, max_length=200)
    binding_id: Optional[uuid.UUID] = None


class SetConversationTitleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)


class SetConversationAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # null → clear the binding (back to plain RAG).
    agent_id: Optional[uuid.UUID] = None


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class ConversationSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    space_id: uuid.UUID
    title: Optional[str]
    status: str
    scope_mode: str = "auto"
    message_count: int
    last_message_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    is_owner: bool = False
    # Per-conversation model override {model_id, binding_id}, or null for
    # the org default. Populated by the router from ``attributes.model``.
    model_choice: Optional[dict] = None
    # Per-conversation agent binding {agent_id}, or null for plain RAG.
    # Populated by the router from ``attributes.agent``.
    agent_choice: Optional[dict] = None


class ConversationList(BaseModel):
    items: list[ConversationSummary]
    total: int


class MessageSummary(BaseModel):
    """One immutable message row."""

    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: dict
    author_user_id: Optional[uuid.UUID]
    status: str
    correlation_id: Optional[str]
    created_at: datetime


class MessageList(BaseModel):
    conversation: ConversationSummary
    items: list[MessageSummary]
    total: int


class SendMessageResponse(BaseModel):
    """Return payload for ``POST /conversations/{id}/messages``."""

    conversation: ConversationSummary
    user_message: MessageSummary
    assistant_message: MessageSummary


# ---------------------------------------------------------------------------
# Knowledge-scope surface (Slice F)
# ---------------------------------------------------------------------------


class SetScopeModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Deliberately not a Literal — the enum belongs to the backend; the
    # service will 400 for anything unknown, keeping the error message in
    # one place.
    mode: str


class AddScopeSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_id: uuid.UUID


class ScopeSourceView(BaseModel):
    """One extra space attached to a conversation as a knowledge source."""

    id: uuid.UUID  # link row id
    conversation_id: uuid.UUID
    space_id: uuid.UUID
    space_name: str
    space_slug: str
    hidden: bool = False
    added_by_user_id: Optional[uuid.UUID]
    created_at: datetime


class ScopeSourceList(BaseModel):
    conversation_id: uuid.UUID
    scope_mode: str
    items: list[ScopeSourceView]
    total: int
