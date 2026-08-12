"""Ghi và đọc telemetry.

Ghi **thẳng** vào DB ngay trong callback thay vì gom lô: một INSERT trên SQLite/WAL rẻ hơn
nhiều so với chính lời gọi LLM vừa xong, còn gom lô thì mất dữ liệu khi tiến trình chết —
mà mất dữ liệu chi phí đúng lúc job ingest dài vỡ giữa chừng là mất phần đáng xem nhất.

Bản ghi tự dọn theo `telemetry_retention_days`: dọn một lần lúc khởi động, rồi cứ mỗi
`_PRUNE_EVERY` lần ghi lại dọn một lần, để một brain chạy nhiều tháng không phình DB.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sag_api.core.config import settings
from sag_api.core.logging import get_logger
from sag_api.core.telemetry import (
    AgentEventRecord,
    LLMCallRecord,
    set_agent_event_sink,
    set_llm_call_sink,
)
from sag_api.db.models import AgentEvent, LLMCall

log = get_logger("telemetry")

#: Dọn định kỳ sau mỗi ngần này bản ghi (rẻ hơn hẳn việc dọn sau từng lần ghi).
_PRUNE_EVERY = 500

_session_factory: async_sessionmaker[AsyncSession] | None = None
_writes_since_prune = 0


def _now() -> datetime:
    return datetime.now(UTC)


def _cutoff() -> datetime:
    return _now() - timedelta(days=max(1, settings.telemetry_retention_days))


async def _maybe_prune(session: AsyncSession) -> None:
    global _writes_since_prune
    _writes_since_prune += 1
    if _writes_since_prune < _PRUNE_EVERY:
        return
    _writes_since_prune = 0
    cutoff = _cutoff()
    await session.execute(delete(LLMCall).where(LLMCall.created_at < cutoff))
    await session.execute(delete(AgentEvent).where(AgentEvent.created_at < cutoff))


async def _write_llm_call(record: LLMCallRecord) -> None:
    if _session_factory is None or not settings.telemetry_enabled:
        return
    async with _session_factory() as session:
        session.add(
            LLMCall(
                created_at=_now(),
                stage=record.stage,
                call_type=record.call_type,
                provider=record.provider,
                model=record.model,
                api_base=record.api_base,
                ok=record.ok,
                failure_kind=record.failure_kind,
                error=record.error,
                latency_ms=record.latency_ms,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                total_tokens=record.total_tokens or (record.input_tokens + record.output_tokens),
                cost_usd=record.cost_usd,
                cost_source=record.cost_source,
                actor=record.actor,
                source_id=record.source_id,
                document_id=record.document_id,
                job_id=record.job_id,
                thread_id=record.thread_id,
                call_id=record.call_id,
            )
        )
        await _maybe_prune(session)
        await session.commit()


async def _write_agent_event(record: AgentEventRecord) -> None:
    if _session_factory is None or not settings.telemetry_enabled:
        return
    async with _session_factory() as session:
        session.add(
            AgentEvent(
                created_at=_now(),
                kind=record.kind,
                actor=record.actor,
                transport=record.transport,
                tool=record.tool,
                query=record.query,
                model=record.model,
                ok=record.ok,
                latency_ms=record.latency_ms,
                result_count=record.result_count,
                result_chars=record.result_chars,
                detail=record.detail or {},
                error=record.error,
            )
        )
        await _maybe_prune(session)
        await session.commit()


def install_telemetry_store(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Cắm tầng ghi DB vào các sink của `core.telemetry`."""
    global _session_factory
    _session_factory = session_factory
    set_llm_call_sink(_write_llm_call)
    set_agent_event_sink(_write_agent_event)


def uninstall_telemetry_store() -> None:
    global _session_factory
    _session_factory = None
    set_llm_call_sink(None)
    set_agent_event_sink(None)


async def prune_now() -> None:
    """Dọn bản ghi quá hạn (gọi lúc khởi động)."""
    if _session_factory is None:
        return
    cutoff = _cutoff()
    async with _session_factory() as session:
        await session.execute(delete(LLMCall).where(LLMCall.created_at < cutoff))
        await session.execute(delete(AgentEvent).where(AgentEvent.created_at < cutoff))
        await session.commit()


# ── Đọc ────────────────────────────────────────────────────────────────────


def _call_row(row: LLMCall) -> dict[str, Any]:
    return {
        "id": row.id,
        "at": row.created_at.isoformat(),
        "stage": row.stage,
        "call_type": row.call_type,
        "provider": row.provider,
        "model": row.model,
        "ok": row.ok,
        "failure_kind": row.failure_kind,
        "error": row.error,
        "latency_ms": row.latency_ms,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "total_tokens": row.total_tokens,
        "cost_usd": row.cost_usd,
        "cost_source": row.cost_source,
        "actor": row.actor,
        "source_id": row.source_id,
        "document_id": row.document_id,
        "job_id": row.job_id,
        "thread_id": row.thread_id,
    }


def _event_row(row: AgentEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "at": row.created_at.isoformat(),
        "kind": row.kind,
        "actor": row.actor,
        "transport": row.transport,
        "tool": row.tool,
        "query": row.query,
        "model": row.model,
        "ok": row.ok,
        "latency_ms": row.latency_ms,
        "result_count": row.result_count,
        "result_chars": row.result_chars,
        "detail": row.detail or {},
        "error": row.error,
    }


async def list_llm_calls(
    session: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
    stage: str | None = None,
    ok: bool | None = None,
    document_id: str | None = None,
) -> dict[str, Any]:
    statement = select(LLMCall)
    counter = select(func.count()).select_from(LLMCall)
    for condition in (
        LLMCall.stage == stage if stage else None,
        LLMCall.ok.is_(ok) if ok is not None else None,
        LLMCall.document_id == document_id if document_id else None,
    ):
        if condition is not None:
            statement = statement.where(condition)
            counter = counter.where(condition)
    rows = (
        (
            await session.execute(
                statement.order_by(LLMCall.created_at.desc(), LLMCall.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    total = int((await session.execute(counter)).scalar_one() or 0)
    return {"total": total, "items": [_call_row(row) for row in rows]}


async def list_agent_events(
    session: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
    kind: str | None = None,
) -> dict[str, Any]:
    statement = select(AgentEvent)
    counter = select(func.count()).select_from(AgentEvent)
    if kind:
        statement = statement.where(AgentEvent.kind == kind)
        counter = counter.where(AgentEvent.kind == kind)
    rows = (
        (
            await session.execute(
                statement.order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    total = int((await session.execute(counter)).scalar_one() or 0)
    return {"total": total, "items": [_event_row(row) for row in rows]}


def _totals_columns() -> tuple:
    known_cost = case((LLMCall.cost_usd.is_(None), 0), else_=1)
    return (
        func.count().label("calls"),
        func.sum(case((LLMCall.ok.is_(True), 1), else_=0)).label("ok_calls"),
        func.sum(LLMCall.input_tokens).label("input_tokens"),
        func.sum(LLMCall.output_tokens).label("output_tokens"),
        func.sum(LLMCall.total_tokens).label("total_tokens"),
        func.sum(func.coalesce(LLMCall.cost_usd, 0.0)).label("cost_usd"),
        func.sum(known_cost).label("priced_calls"),
    )


def _totals_dict(row: Any) -> dict[str, Any]:
    calls = int(row.calls or 0)
    priced = int(row.priced_calls or 0)
    return {
        "calls": calls,
        "ok_calls": int(row.ok_calls or 0),
        "failed_calls": calls - int(row.ok_calls or 0),
        "input_tokens": int(row.input_tokens or 0),
        "output_tokens": int(row.output_tokens or 0),
        "total_tokens": int(row.total_tokens or 0),
        "cost_usd": round(float(row.cost_usd or 0.0), 6),
        # Bao nhiêu lời gọi thật sự có giá. Phần còn lại KHÔNG được coi là 0 đồng —
        # UI phải nói rõ "chưa biết giá" chứ không được cộng dồn thành miễn phí.
        "priced_calls": priced,
        "unpriced_calls": calls - priced,
    }


async def summary(session: AsyncSession, *, days: int = 7) -> dict[str, Any]:
    """Tổng hợp chi phí/token theo khoảng ngày, kèm chia nhỏ theo stage · model · ngày."""
    since = _now() - timedelta(days=max(1, days))
    scope = LLMCall.created_at >= since

    totals = (await session.execute(select(*_totals_columns()).where(scope))).one()

    by_stage = [
        {"key": row.stage, **_totals_dict(row)}
        for row in (
            await session.execute(
                select(LLMCall.stage.label("stage"), *_totals_columns())
                .where(scope)
                .group_by(LLMCall.stage)
                .order_by(func.count().desc())
            )
        ).all()
    ]
    by_model = [
        {"key": row.model or "(unknown)", "provider": row.provider, **_totals_dict(row)}
        for row in (
            await session.execute(
                select(LLMCall.model.label("model"), LLMCall.provider.label("provider"), *_totals_columns())
                .where(scope)
                .group_by(LLMCall.model, LLMCall.provider)
                .order_by(func.count().desc())
                .limit(20)
            )
        ).all()
    ]

    # Gộp theo ngày ở Python: `date()` của SQLite và của Postgres không cùng cú pháp,
    # và số hàng trong một khoảng ngày luôn nhỏ nên không cần đẩy xuống DB.
    daily: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "total_tokens": 0, "cost_usd": 0.0, "priced_calls": 0}
    )
    for created_at, total_tokens, cost in (
        await session.execute(
            select(LLMCall.created_at, LLMCall.total_tokens, LLMCall.cost_usd).where(scope)
        )
    ).all():
        bucket = daily[created_at.date().isoformat()]
        bucket["calls"] += 1
        bucket["total_tokens"] += int(total_tokens or 0)
        if cost is not None:
            bucket["cost_usd"] = round(bucket["cost_usd"] + float(cost), 6)
            bucket["priced_calls"] += 1
    by_day = [{"key": day, **values} for day, values in sorted(daily.items())]

    knowledge_scope = AgentEvent.created_at >= since
    events = (
        await session.execute(
            select(
                AgentEvent.kind,
                func.count().label("count"),
                func.sum(AgentEvent.result_count).label("results"),
                func.sum(case((AgentEvent.ok.is_(True), 0), else_=1)).label("failed"),
            )
            .where(knowledge_scope)
            .group_by(AgentEvent.kind)
        )
    ).all()
    by_tool = [
        {"key": row.tool or "(unknown)", "count": int(row.count or 0), "results": int(row.results or 0)}
        for row in (
            await session.execute(
                select(
                    AgentEvent.tool,
                    func.count().label("count"),
                    func.sum(AgentEvent.result_count).label("results"),
                )
                .where(knowledge_scope, AgentEvent.kind == "knowledge_call")
                .group_by(AgentEvent.tool)
                .order_by(func.count().desc())
            )
        ).all()
    ]
    by_actor = [
        {"key": row.actor or "unknown", "count": int(row.count or 0)}
        for row in (
            await session.execute(
                select(AgentEvent.actor, func.count().label("count"))
                .where(knowledge_scope)
                .group_by(AgentEvent.actor)
                .order_by(func.count().desc())
                .limit(20)
            )
        ).all()
    ]

    return {
        "days": max(1, days),
        "since": since.isoformat(),
        "retention_days": settings.telemetry_retention_days,
        "enabled": settings.telemetry_enabled,
        "totals": _totals_dict(totals),
        "by_stage": by_stage,
        "by_model": by_model,
        "by_day": by_day,
        "agent": {
            "by_kind": [
                {
                    "key": row.kind,
                    "count": int(row.count or 0),
                    "results": int(row.results or 0),
                    "failed": int(row.failed or 0),
                }
                for row in events
            ],
            "by_tool": by_tool,
            "by_actor": by_actor,
        },
    }


async def purge(session: AsyncSession) -> dict[str, int]:
    """Xoá sạch telemetry (nút "Clear" trên UI)."""
    calls = int((await session.execute(select(func.count()).select_from(LLMCall))).scalar_one() or 0)
    events = int((await session.execute(select(func.count()).select_from(AgentEvent))).scalar_one() or 0)
    await session.execute(delete(LLMCall))
    await session.execute(delete(AgentEvent))
    await session.commit()
    return {"llm_calls": calls, "agent_events": events}
