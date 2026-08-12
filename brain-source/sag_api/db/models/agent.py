from __future__ import annotations

from sqlalchemy import JSON, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from sag_api.db.base import Base, IDMixin, TimestampMixin
from sag_api.enums import BindingTargetType, MessageRole


class Agent(IDMixin, TimestampMixin, Base):
    """Agent - a name + a system prompt + the sources/tools it mounts (through MCP)."""

    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(String(120))
    avatar: Mapped[str] = mapped_column(String(64), default="")  # emoji / initial
    # Default agent: the out-of-the-box main conversation entry, knowledge base = every source (special-cased in resolve_sources)
    is_default: Mapped[bool] = mapped_column(default=False, index=True)
    # Config: { system_prompt, greeting, tools[] } (tools holds the extra enabled tool/MCP names)
    persona: Mapped[dict] = mapped_column("persona_json", JSON, default=dict)


class AgentBinding(IDMixin, TimestampMixin, Base):
    """What an Agent mounts: one source, or one MCP server (a tool provider)."""

    __tablename__ = "agent_bindings"
    __table_args__ = (UniqueConstraint("agent_id", "target_type", "target_id", name="uq_agent_binding"),)

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    target_type: Mapped[BindingTargetType] = mapped_column(SAEnum(BindingTargetType, native_enum=False, length=16))
    target_id: Mapped[str] = mapped_column(String(64), index=True)
    # MCP server connection config (url, or command/args/env); empty for a source binding
    config: Mapped[dict] = mapped_column("config_json", JSON, default=dict)


class Thread(IDMixin, TimestampMixin, Base):
    __tablename__ = "threads"

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300), default="New chat")
    archived: Mapped[bool] = mapped_column(default=False, index=True)


class Message(IDMixin, TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_thread_created_id", "thread_id", "created_at", "id"),)

    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"))
    # Image attachment meta: [{id, name, media_type}] (files live in upload_dir/attachments/)
    attachments: Mapped[list] = mapped_column("attachments_json", JSON, default=list)
    # Agentic execution trace: [{kind:thinking|tool, step, name?, args?, ms, count?}] (assistant messages)
    steps: Mapped[list] = mapped_column("steps_json", JSON, default=list)
    role: Mapped[MessageRole] = mapped_column(SAEnum(MessageRole, native_enum=False, length=16))
    content: Mapped[str] = mapped_column(Text, default="")
    citations: Mapped[list] = mapped_column("citations_json", JSON, default=list)
    # Frozen initial model input for this assistant turn. It deliberately
    # excludes tool results and the generated answer so historical playback
    # can audit the same role-separated input that was shown live.
    prompt_preview: Mapped[str] = mapped_column(Text, default="")
