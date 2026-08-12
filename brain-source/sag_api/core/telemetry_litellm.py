"""Bắt **mọi** request LLM tại đúng một chỗ: callback của LiteLLM.

Vì sao đặt ở đây chứ không bọc `LLMClient`: đường trích xuất chạy trong `alicecore`, nó tự
gọi LiteLLM bên trong dependency. Bọc ở tầng API sẽ bỏ sót đúng phần tốn tiền nhất
(tinh luyện tài liệu). LiteLLM cho phép cắm `CustomLogger` toàn tiến trình, và bản thân nó
đã dựng sẵn `standard_logging_object` gồm token và chi phí — nên một hook duy nhất thấy
được cả chat lẫn trích xuất, không phải vá `site-packages`.

Chi phí: `response_cost` của LiteLLM tính từ bảng giá của chính nó. Model lạ (gateway tự
host, tên model không có trong bảng) trả về `0.0` — trường hợp đó ghi `cost_usd = None`
kèm `cost_source = "unknown"`. **Không** ghi 0.0, vì "không biết giá" khác "miễn phí".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sag_api.core.llm_routing import classify_failure
from sag_api.core.telemetry import (
    STAGE_EMBEDDING,
    STAGE_GENERATION,
    LLMCallRecord,
    emit_llm_call,
)

_EMBEDDING_CALL_TYPES = {"embedding", "aembedding"}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _latency_ms(payload: dict[str, Any], start: Any, end: Any) -> int:
    response_time = payload.get("response_time")
    if isinstance(response_time, (int, float)) and response_time > 0:
        return int(response_time * 1000)
    started, ended = payload.get("startTime"), payload.get("endTime")
    if isinstance(started, (int, float)) and isinstance(ended, (int, float)):
        return max(0, int((ended - started) * 1000))
    if isinstance(start, datetime) and isinstance(end, datetime):
        return max(0, int((end - start).total_seconds() * 1000))
    return 0


def _cost(payload: dict[str, Any], ok: bool) -> tuple[float | None, str]:
    if not ok:
        return None, "unknown"
    raw = payload.get("response_cost")
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw), "litellm"
    return None, "unknown"


def _record_from(kwargs: dict[str, Any], start: Any, end: Any, *, ok: bool) -> LLMCallRecord:
    payload = kwargs.get("standard_logging_object") or {}
    call_type = str(payload.get("call_type") or kwargs.get("call_type") or "acompletion")
    model = str(payload.get("model") or kwargs.get("model") or "")
    api_base = payload.get("api_base") or (kwargs.get("litellm_params") or {}).get("api_base")
    cost, cost_source = _cost(payload, ok)

    failure_kind: str | None = None
    error: str | None = None
    if not ok:
        exception = kwargs.get("exception")
        error = str(payload.get("error_str") or exception or "")[:500] or None
        source = exception if isinstance(exception, BaseException) else RuntimeError(error or "")
        failure_kind = classify_failure(source).value

    return LLMCallRecord(
        stage=STAGE_EMBEDDING if call_type in _EMBEDDING_CALL_TYPES else STAGE_GENERATION,
        call_type=call_type,
        provider=str(payload.get("custom_llm_provider") or ""),
        model=model,
        api_base=str(api_base)[:300] if api_base else None,
        ok=ok,
        failure_kind=failure_kind,
        error=error,
        latency_ms=_latency_ms(payload, start, end),
        input_tokens=_int(payload.get("prompt_tokens")),
        output_tokens=_int(payload.get("completion_tokens")),
        total_tokens=_int(payload.get("total_tokens")),
        cost_usd=cost,
        cost_source=cost_source,
        call_id=str(payload.get("litellm_call_id") or "")[:64] or None,
    )


def install_litellm_telemetry() -> Any:
    """Cắm logger telemetry vào LiteLLM và trả về handle để gỡ khi tắt app."""

    import litellm
    from litellm.integrations.custom_logger import CustomLogger

    class TelemetryLogger(CustomLogger):
        async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001
            await emit_llm_call(_record_from(kwargs, start_time, end_time, ok=True))

        async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001
            await emit_llm_call(_record_from(kwargs, start_time, end_time, ok=False))

    logger = TelemetryLogger()
    litellm.callbacks.append(logger)
    return logger


def uninstall_litellm_telemetry(logger: Any) -> None:
    import litellm

    if logger in litellm.callbacks:
        litellm.callbacks.remove(logger)
