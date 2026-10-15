"""Pydantic contracts for the agent endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Agent config
# ---------------------------------------------------------------------------


class AgentSummary(BaseModel):
    """An agent persona as shown in the list + selected header."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: Optional[str] = None
    icon: str = "bot"
    enabled_tools: list[str] = Field(default_factory=list)
    knowledge_scope_mode: str = "user"
    scope_space_ids: list[str] = Field(default_factory=list)
    visibility: str = "organization"
    is_default: bool = False
    created_at: datetime
    # Kind + external config (derived by the router from the ORM row).
    kind: str = "builtin"
    external_protocol: Optional[str] = None
    external_endpoint: Optional[str] = None
    external_model: Optional[str] = None
    external_bot_id: Optional[str] = None
    external_api_key_present: bool = False


class AgentDetail(AgentSummary):
    """Full config, including the editable system prompt."""

    system_prompt: str = ""


class AgentList(BaseModel):
    items: list[AgentSummary]
    total: int


class CreateAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)
    icon: Optional[str] = Field(default=None, max_length=48)
    system_prompt: Optional[str] = Field(default=None, max_length=4000)
    # Builtin agents need at least one tool; external agents run no tools,
    # so this may be empty. The service enforces the builtin case.
    enabled_tools: list[str] = Field(default_factory=list)
    knowledge_scope_mode: Optional[str] = None
    scope_space_ids: Optional[list[uuid.UUID]] = None
    visibility: Optional[str] = None
    # External agent (kind='external'):
    kind: Optional[str] = None
    external_protocol: Optional[str] = Field(default=None, max_length=32)
    external_endpoint: Optional[str] = Field(default=None, max_length=500)
    external_model: Optional[str] = Field(default=None, max_length=200)
    external_bot_id: Optional[str] = Field(default=None, max_length=128)
    external_extra: Optional[dict] = None
    external_api_key: Optional[str] = Field(default=None, max_length=1000)


class UpdateAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)
    icon: Optional[str] = Field(default=None, max_length=48)
    system_prompt: Optional[str] = Field(default=None, max_length=4000)
    enabled_tools: Optional[list[str]] = Field(default=None)
    knowledge_scope_mode: Optional[str] = None
    scope_space_ids: Optional[list[uuid.UUID]] = None
    visibility: Optional[str] = None
    kind: Optional[str] = None
    external_protocol: Optional[str] = Field(default=None, max_length=32)
    external_endpoint: Optional[str] = Field(default=None, max_length=500)
    external_model: Optional[str] = Field(default=None, max_length=200)
    external_bot_id: Optional[str] = Field(default=None, max_length=128)
    external_extra: Optional[dict] = None
    external_api_key: Optional[str] = Field(default=None, max_length=1000)


class ToolInfo(BaseModel):
    name: str
    description: str


class ToolCatalog(BaseModel):
    items: list[ToolInfo]


# ---------------------------------------------------------------------------
# Access grants (who may use a restricted agent)
# ---------------------------------------------------------------------------


class AgentAccessGrant(BaseModel):
    id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    subject_display_name: str
    subject_subtitle: Optional[str] = None
    created_at: datetime


class AgentAccessList(BaseModel):
    items: list[AgentAccessGrant]
    total: int


class GrantAgentAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_type: str
    subject_id: uuid.UUID


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


class RunAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=4000)


class RunAgentResponse(BaseModel):
    answer: str
    iterations: int
    model_id: str
    tool_call_count: int
    trace: list[dict[str, Any]]
