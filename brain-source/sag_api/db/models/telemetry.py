"""Bảng telemetry: mọi lời gọi LLM và mọi lần agent chạm vào tri thức.

Hai bảng này là **nhật ký chỉ-thêm**, cố ý **không** có khoá ngoại tới `documents`/`sources`:
xoá một tài liệu không được xoá mất chi phí đã tiêu cho nó, nếu không thì câu hỏi
"tháng này tinh luyện tốn bao nhiêu" sẽ trả lời sai ngay khi người dùng dọn kho.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from sag_api.db.base import Base, IDMixin, UTCDateTime


class LLMCall(IDMixin, Base):
    """Một request tới nhà cung cấp LLM (chat, trích xuất, embedding, probe)."""

    __tablename__ = "llm_calls"
    __table_args__ = (
        Index("ix_llm_calls_created_stage", "created_at", "stage"),
        Index("ix_llm_calls_document", "document_id"),
    )

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now(), nullable=False, index=True)
    #: extraction | generation | embedding | probe | other — vùng nghiệp vụ đã gọi
    stage: Mapped[str] = mapped_column(String(24), default="generation", index=True)
    #: acompletion | aembedding | … (call_type của litellm)
    call_type: Mapped[str] = mapped_column(String(32), default="acompletion")
    provider: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(200), default="", index=True)
    api_base: Mapped[str | None] = mapped_column(String(300), nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    #: Phân loại lỗi theo `core.llm_routing.FailureKind` (chỉ có khi ok=False)
    failure_kind: Mapped[str | None] = mapped_column(String(24), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    #: Chi phí USD. **NULL nghĩa là không biết**, không phải bằng 0 — gateway tự host
    #: hoặc model lạ không có bảng giá, đừng để UI cộng nhầm thành "miễn phí".
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: litellm | unknown — nguồn của con số chi phí, để UI nói thật nó lấy đâu ra
    cost_source: Mapped[str] = mapped_column(String(16), default="unknown")
    #: ingest | chat | probe | mcp | … — ai châm ngòi lời gọi này
    actor: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    document_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: litellm_call_id — đối chiếu được với log của chính litellm khi cần soi sâu
    call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AgentEvent(IDMixin, Base):
    """Một lần agent làm việc **qua** brain: đọc/ghi tri thức, hoặc giao việc cho sub-agent."""

    __tablename__ = "agent_events"
    __table_args__ = (Index("ix_agent_events_created_kind", "created_at", "kind"),)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now(), nullable=False, index=True)
    #: knowledge_call | knowledge_write | sub_agent_registry | delegation
    kind: Mapped[str] = mapped_column(String(24), default="knowledge_call", index=True)
    #: Ai gọi: nhãn client MCP (claude-code, codex…), "agent" cho vòng chạy nội bộ
    actor: Mapped[str] = mapped_column(String(120), default="unknown")
    #: http | stdio | inproc — đường vào MCP
    transport: Mapped[str] = mapped_column(String(16), default="http")
    #: Tên tool tri thức (search/grep/…) hoặc slot sub-agent với bản ghi delegation
    tool: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Câu hỏi đã tra, hoặc mô tả việc đã giao
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Model của sub-agent (chỉ dùng cho delegation)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    #: Số kết quả trả về và độ dài văn bản — "agent lấy được bao nhiêu tri thức"
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    result_chars: Mapped[int] = mapped_column(Integer, default=0)
    #: Chi tiết gọn: tham số, chunk_id/document_id đã chạm, trích đoạn đầu kết quả
    detail: Mapped[dict] = mapped_column("detail_json", JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
