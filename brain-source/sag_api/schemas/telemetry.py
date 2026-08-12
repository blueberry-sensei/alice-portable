from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class AgentTaskLogRequest(BaseModel):
    """Một lần orchestrator khai báo đã giao việc cho sub-agent (brain không tự thấy được)."""

    agent: str = Field(min_length=1, max_length=120)
    task: str = Field(min_length=1, max_length=2000)
    status: Literal["started", "done", "failed"] = "done"
    model: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=2000)
    #: Nhãn của chính orchestrator (claude-code, codex…), khác với `agent` là bên nhận việc.
    actor: str = Field(default="api", max_length=120)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)


class KnowledgeSyncLogRequest(BaseModel):
    """Một lần sync đã đưa thay đổi tri thức từ filesystem vào Brain."""

    actor: str = Field(default="alice-sync", min_length=1, max_length=120)
    source_id: str = Field(min_length=1, max_length=64)
    source_name: str = Field(min_length=1, max_length=200)
    created: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=200)
    updated: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=200)
    deleted: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=200)
    skipped: int = Field(default=0, ge=0)
    rebuild: bool = False
