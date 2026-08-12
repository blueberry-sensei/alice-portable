"""Telemetry: chi phí/token của mọi lời gọi LLM, và dấu vết agent lấy tri thức.

Đọc-chỉ, trừ `DELETE` để dọn sạch. Mọi endpoint yêu cầu đăng nhập — dữ liệu này lộ ra
câu hỏi người dùng đã tra và mô hình đang dùng, không phải thứ để mở công khai.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user
from sag_api.core.telemetry import AgentEventRecord, emit_agent_event
from sag_api.db.models import User
from sag_api.schemas.telemetry import AgentTaskLogRequest, KnowledgeSyncLogRequest
from sag_api.services import telemetry_service

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


@router.get("/summary")
async def get_summary(
    days: int = Query(default=7, ge=1, le=365),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Tổng token/chi phí trong `days` ngày, chia theo stage · model · ngày, kèm hoạt động agent."""
    return await telemetry_service.summary(session, days=days)


@router.get("/llm-calls")
async def get_llm_calls(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    stage: str | None = Query(default=None),
    ok: bool | None = Query(default=None),
    document_id: str | None = Query(default=None),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await telemetry_service.list_llm_calls(
        session, limit=limit, offset=offset, stage=stage, ok=ok, document_id=document_id
    )


@router.get("/agent-events")
async def get_agent_events(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    kind: str | None = Query(default=None),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await telemetry_service.list_agent_events(session, limit=limit, offset=offset, kind=kind)


@router.post("/agent-events", status_code=201)
async def log_agent_task(
    body: AgentTaskLogRequest,
    _user: User = Depends(get_current_user),
) -> dict:
    """Đường HTTP tương đương tool MCP `log_agent_task`.

    Dành cho orchestrator có sẵn token HTTP (script CI, agent không cắm MCP). Số token và
    chi phí ở đây là **do agent khai**, brain không đo được sub-agent chạy ngoài máy.
    """
    recorded = settings.telemetry_enabled and await emit_agent_event(
        AgentEventRecord(
            kind="delegation",
            actor=body.actor or "api",
            transport="http",
            tool=body.agent,
            query=body.task,
            model=body.model or None,
            ok=body.status != "failed",
            detail={
                "status": body.status,
                "note": body.note,
                "input_tokens": body.input_tokens,
                "output_tokens": body.output_tokens,
                "cost_usd": body.cost_usd,
                "cost_source": "reported",
            },
        )
    )
    return {"ok": recorded}


@router.post("/knowledge-sync", status_code=201)
async def log_knowledge_sync(
    body: KnowledgeSyncLogRequest,
    _user: User = Depends(get_current_user),
) -> dict:
    """Ghi đúng diff mà sync đã nhận; không có thay đổi thì không tạo event nhiễu."""
    changed = len(body.created) + len(body.updated) + len(body.deleted)
    if changed == 0:
        return {"ok": True, "recorded": False}

    lines = [
        *[f"+ {path[:500]}" for path in body.created],
        *[f"~ {path[:500]}" for path in body.updated],
        *[f"- {path[:500]}" for path in body.deleted],
    ]
    preview = "\n".join(lines[:50])
    if len(lines) > 50:
        preview += f"\n... and {len(lines) - 50} more"

    recorded = settings.telemetry_enabled and await emit_agent_event(
        AgentEventRecord(
            kind="knowledge_write",
            actor=body.actor,
            transport="http",
            tool="sync",
            query=body.source_name,
            ok=True,
            result_count=changed,
            result_chars=sum(len(path) for path in body.created + body.updated + body.deleted),
            detail={
                "status": "done",
                "source_id": body.source_id,
                "source_name": body.source_name,
                "created": body.created,
                "updated": body.updated,
                "deleted": body.deleted,
                "created_count": len(body.created),
                "updated_count": len(body.updated),
                "deleted_count": len(body.deleted),
                "skipped_count": body.skipped,
                "rebuild": body.rebuild,
                "preview": preview,
            },
        )
    )
    return {"ok": recorded, "recorded": recorded}


@router.delete("")
async def purge_telemetry(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Xoá sạch telemetry đã ghi (số trả về là số bản ghi vừa xoá)."""
    return await telemetry_service.purge(session)
