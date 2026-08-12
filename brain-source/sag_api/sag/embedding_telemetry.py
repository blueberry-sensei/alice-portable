"""Nối usage embedding của engine về telemetry của API.

Embedding **không** đi qua LiteLLM (engine gọi thẳng SDK openai), nên callback LiteLLM
không thấy nó. Nếu bỏ qua, bảng chi phí sẽ thiếu đúng phần chạy nhiều nhất lúc ingest.

Sink của engine là hàm đồng bộ, còn ghi telemetry là bất đồng bộ, nên bản ghi được đẩy
sang một task nền. Giữ tham chiếu tới task trong một `set` vì `asyncio` chỉ giữ
weak-reference: mất tham chiếu là task có thể bị thu gom trước khi chạy xong.

Đặt trong `sag/` vì đây là nơi duy nhất được phép import `alicecore`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from alicecore.core.ai import embedding as engine_embedding

from sag_api.core.logging import get_logger
from sag_api.core.telemetry import STAGE_EMBEDDING, LLMCallRecord, current_context, emit_llm_call

log = get_logger("telemetry")

# Sink usage của embedding chỉ có từ alicecore mới. Import CỨNG ở đây từng làm cả API chết lúc
# khởi động khi image nhúng alicecore cũ (image api và image engine build song song — bẫy đã ghi
# ở AGENTS.md). Telemetry là tính năng phụ; nó **không được phép** kéo sập cả ứng dụng.
#
# Import module trước rồi mới getattr: module embedding là contract cũ đã tồn tại. Cách này chỉ
# hạ cấp khi đúng hook mới chưa có, không nuốt một ImportError thật phát sinh bên trong module.
set_embedding_usage_sink = getattr(engine_embedding, "set_embedding_usage_sink", None)

_pending: set[asyncio.Task] = set()


def _to_record(usage: Any) -> LLMCallRecord:
    return LLMCallRecord(
        stage=STAGE_EMBEDDING,
        call_type="aembedding",
        provider="openai-compatible",
        model=usage.model,
        api_base=(usage.base_url or None),
        ok=usage.ok,
        error=usage.error,
        latency_ms=usage.latency_ms,
        input_tokens=usage.prompt_tokens,
        output_tokens=0,
        total_tokens=usage.total_tokens or usage.prompt_tokens,
        # Không có bảng giá cho endpoint embedding tự cấu hình (mặc định còn là ollama cục bộ):
        # ghi None = "không biết", tuyệt đối không ghi 0 để khỏi tưởng là miễn phí.
        cost_usd=None,
        cost_source="unknown",
    )


def _handle(usage: Any) -> None:
    record = _to_record(usage)
    # Ngữ cảnh (document/job đang ingest) phải chụp NGAY tại đây, vì task nền chạy sau.
    context = current_context()
    record.stage = STAGE_EMBEDDING
    record.actor = context.actor
    record.source_id = context.source_id
    record.document_id = context.document_id
    record.job_id = context.job_id
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # ngoài event loop (script đồng bộ) — bỏ qua, không có chỗ để ghi
    task = loop.create_task(emit_llm_call(record))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def embedding_telemetry_supported() -> bool:
    """Engine hiện tại có báo usage embedding không (alicecore cũ thì không)."""
    return set_embedding_usage_sink is not None


def install_engine_embedding_telemetry() -> None:
    if set_embedding_usage_sink is None:
        # Nói thẳng ra log thay vì im lặng: chi phí embedding sẽ KHÔNG có trong telemetry,
        # và người đọc cần biết lý do là engine cũ chứ không phải telemetry hỏng.
        log.warning(
            "This alicecore build does not report embedding usage; embedding cost stays out of telemetry"
        )
        return
    set_embedding_usage_sink(_handle)


def uninstall_engine_embedding_telemetry() -> None:
    if set_embedding_usage_sink is None:
        return
    set_embedding_usage_sink(None)
