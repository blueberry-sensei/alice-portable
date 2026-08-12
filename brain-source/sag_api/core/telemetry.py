"""Lõi telemetry: ngữ cảnh của lời gọi + đường dẫn bản ghi ra ngoài.

Module này **không** biết database. Nó chỉ giữ hai thứ:

1. **Ngữ cảnh** (`TelemetryContext`) — ai đang gọi và vì việc gì. Lưu bằng `contextvars`
   nên mọi task con sinh ra bên trong đều thừa hưởng: job ingest đặt một lần, cả chuỗi
   trích xuất phía dưới (kể cả lời gọi litellm nằm trong `alicecore`) đều mang đúng
   `document_id`/`job_id` mà không phải truyền tay qua chục tầng.
2. **Sink** — nơi nhận bản ghi. Mặc định là `None` (không ghi gì), tầng service cắm bản
   ghi-DB vào lúc khởi động. Nhờ vậy test đơn vị soi được bản ghi mà không cần DB, và
   một lỗi ở tầng ghi **không bao giờ** làm hỏng lời gọi LLM đang chạy.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any

from sag_api.core.logging import get_logger

log = get_logger("telemetry")

#: Vùng nghiệp vụ của một lời gọi LLM.
STAGE_EXTRACTION = "extraction"
STAGE_GENERATION = "generation"
STAGE_EMBEDDING = "embedding"
STAGE_PROBE = "probe"


@dataclass(frozen=True)
class TelemetryContext:
    """Ngữ cảnh gắn vào mọi bản ghi sinh ra bên trong nó."""

    stage: str = STAGE_GENERATION
    actor: str | None = None
    source_id: str | None = None
    document_id: str | None = None
    job_id: str | None = None
    thread_id: str | None = None


_context: contextvars.ContextVar[TelemetryContext] = contextvars.ContextVar(
    "sag_telemetry_context", default=TelemetryContext()
)


def current_context() -> TelemetryContext:
    return _context.get()


@contextlib.contextmanager
def use_context(**overrides: Any) -> Iterator[TelemetryContext]:
    """Đặt ngữ cảnh cho khối lệnh bên trong (kế thừa phần không ghi đè)."""
    scoped = replace(current_context(), **overrides)
    token = _context.set(scoped)
    try:
        yield scoped
    finally:
        _context.reset(token)


@dataclass
class LLMCallRecord:
    """Một request tới nhà cung cấp LLM — thành công hay thất bại đều ghi."""

    stage: str = STAGE_GENERATION
    call_type: str = "acompletion"
    provider: str = ""
    model: str = ""
    api_base: str | None = None
    ok: bool = True
    failure_kind: str | None = None
    error: str | None = None
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    #: None = **không biết giá**, khác hẳn 0.0 = biết chắc miễn phí.
    cost_usd: float | None = None
    cost_source: str = "unknown"
    actor: str | None = None
    source_id: str | None = None
    document_id: str | None = None
    job_id: str | None = None
    thread_id: str | None = None
    call_id: str | None = None


@dataclass
class AgentEventRecord:
    """Một lần agent làm việc qua brain: đọc/ghi tri thức, hoặc giao việc cho sub-agent."""

    kind: str = "knowledge_call"
    actor: str = "unknown"
    transport: str = "http"
    tool: str | None = None
    query: str | None = None
    model: str | None = None
    ok: bool = True
    latency_ms: int = 0
    result_count: int = 0
    result_chars: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


LLMCallSink = Callable[[LLMCallRecord], Awaitable[None]]
AgentEventSink = Callable[[AgentEventRecord], Awaitable[None]]

_llm_sink: LLMCallSink | None = None
_agent_sink: AgentEventSink | None = None


def set_llm_call_sink(sink: LLMCallSink | None) -> None:
    global _llm_sink
    _llm_sink = sink


def set_agent_event_sink(sink: AgentEventSink | None) -> None:
    global _agent_sink
    _agent_sink = sink


def _apply_context(record: LLMCallRecord) -> LLMCallRecord:
    context = current_context()
    if record.stage == STAGE_GENERATION and context.stage != STAGE_GENERATION:
        record.stage = context.stage
    record.actor = record.actor or context.actor
    record.source_id = record.source_id or context.source_id
    record.document_id = record.document_id or context.document_id
    record.job_id = record.job_id or context.job_id
    record.thread_id = record.thread_id or context.thread_id
    return record


async def emit_llm_call(record: LLMCallRecord) -> None:
    """Gửi bản ghi đi. Lỗi ở tầng ghi chỉ được log, **không** ném ngược lên lời gọi LLM."""
    sink = _llm_sink
    if sink is None:
        return
    try:
        await sink(_apply_context(record))
    except Exception as error:  # noqa: BLE001 - telemetry không được phép làm hỏng nghiệp vụ
        log.warning("Failed to record an LLM call: %s", error)


async def emit_agent_event(record: AgentEventRecord) -> bool:
    """Gửi bản ghi hoạt động; trả False khi không có sink hoặc tầng ghi thất bại."""
    sink = _agent_sink
    if sink is None:
        return False
    try:
        await sink(record)
        return True
    except Exception as error:  # noqa: BLE001
        log.warning("Failed to record an agent event: %s", error)
        return False
