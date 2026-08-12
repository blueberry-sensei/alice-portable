from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sag_api.enums import BindingTargetType, MessageRole


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    avatar: str = ""
    persona: dict[str, Any] = Field(default_factory=dict)  # { system_prompt, greeting, tools[] }


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    avatar: str | None = None
    persona: dict[str, Any] | None = None


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    avatar: str
    persona: dict[str, Any]
    is_default: bool = False
    created_at: datetime
    updated_at: datetime


class BindingCreate(BaseModel):
    target_type: BindingTargetType = BindingTargetType.SOURCE
    target_id: str = ""
    config: dict[str, Any] = Field(default_factory=dict)  # MCP: url, or command/args/env


class BindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    target_type: BindingTargetType
    target_id: str
    config: dict[str, Any]


class ThreadCreate(BaseModel):
    title: str = "New chat"


class ThreadUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    archived: bool | None = None


class ThreadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    title: str
    archived: bool = False
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    thread_id: str
    role: MessageRole
    content: str
    citations: list[dict[str, Any]]
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    prompt_preview: str = ""
    created_at: datetime


class MessagePageOut(BaseModel):
    items: list[MessageOut] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class AskRequest(BaseModel):
    query: str = Field(default="", max_length=4000)
    # Image attachment ids (<=4, uploaded through POST /attachments)
    attachments: list[str] = Field(default_factory=list, max_length=4)
    # @knowledge-base scope: search only within these sources (empty = every source)
    source_ids: list[str] = Field(default_factory=list, max_length=8)
    # Web access is granted by the user per turn; off by default, and only then are the Agent's external/MCP tools exposed.
    web_enabled: bool = False
    # Legacy client compatibility field: knowledge_only=false is equivalent to web_enabled=true.
    # When both appear, the semantically clearer web_enabled wins.
    knowledge_only: bool | None = None

    @model_validator(mode="after")
    def require_text_or_attachment(self):
        if not self.query.strip() and not self.attachments:
            raise ValueError("Provide at least one of a question or an image")
        return self

    @property
    def effective_web_enabled(self) -> bool:
        """Resolve the new opt-in switch while preserving explicit legacy requests."""

        if "web_enabled" in self.model_fields_set:
            return self.web_enabled
        if self.knowledge_only is not None:
            return not self.knowledge_only
        return False


class ToolRejection(BaseModel):
    reason: str = Field(default="The user denied execution", max_length=500)
