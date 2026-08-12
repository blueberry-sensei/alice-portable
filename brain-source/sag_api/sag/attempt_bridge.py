"""Nối log định tuyến provider của engine về API.

Engine `alicecore` tự chuyển provider khi trích xuất, và nó báo mọi lần thử qua một sink
toàn tiến trình. Cầu nối này đổ các bản ghi đó vào cùng một `ATTEMPT_LOG` với đường
chat/answer, nhờ vậy UI chỉ cần đọc **một** nguồn để trả lời "provider nào vừa fail, vì sao".

Đặt trong `sag/` vì đây là nơi duy nhất được phép import `alicecore`.
"""

from __future__ import annotations

from alicecore.core.ai.routing import AttemptRecord as EngineAttemptRecord
from alicecore.core.ai.routing import set_attempt_sink

from sag_api.core.llm_routing import AttemptRecord, record_attempt


def _to_api_record(record: EngineAttemptRecord) -> AttemptRecord:
    return AttemptRecord(
        provider_id=record.provider_id,
        label=record.label,
        model=record.model,
        stage="extraction",
        attempt=record.attempt,
        ok=record.ok,
        action=record.action,
        latency_ms=record.latency_ms,
        kind=record.kind.value if record.kind else None,
        error=record.error,
    )


def install_engine_attempt_bridge() -> None:
    set_attempt_sink(lambda record: record_attempt(_to_api_record(record)))


def uninstall_engine_attempt_bridge() -> None:
    set_attempt_sink(None)
