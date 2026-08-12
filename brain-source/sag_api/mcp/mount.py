"""Mount the SAG knowledge-base MCP into FastAPI as a Streamable-HTTP endpoint.

External hosts (Claude Desktop / Cursor) can mount:

    http://<host>/mcp/                          # the whole knowledge base
    http://<host>/mcp/?source_id=<source id>    # a single source

A request is authenticated by JWT first, then one or every Source is loaded according to the optional
`source_id` and injected into a contextvar. The scope is isolated per request, so external hosts and the in-process agent can share one server.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

import jwt
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select

from sag_api.core.db import SessionLocal
from sag_api.core.logging import get_logger
from sag_api.core.security import decode_token
from sag_api.db.models import Source
from sag_api.mcp.server import build_source_mcp, use_scope

if TYPE_CHECKING:
    from fastapi import FastAPI
    from mcp.server.fastmcp import FastMCP

log = get_logger("mcp.http")


async def _send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key == name:
            return value.decode("latin-1").strip() or None
    return None


def _actor(scope, params: dict[str, list[str]]) -> str:
    """Nhãn của client MCP, chỉ dùng cho telemetry.

    Không có cách chuẩn nào để biết tên host MCP ở tầng ASGI, nên nhận theo thứ tự:
    `?actor=`, header `x-alice-actor`, rồi mới tới `user-agent`. Đây là dữ liệu do client
    khai — dùng để phân biệt "ai đang tra cứu", **không** dùng cho phân quyền.
    """
    declared = (params.get("actor") or [""])[0].strip()
    label = declared or _header(scope, b"x-alice-actor") or _header(scope, b"user-agent") or "mcp-http"
    return label[:120]


def _bearer(scope) -> str | None:
    for name, value in scope.get("headers") or []:
        if name == b"authorization":
            raw = value.decode("latin-1")
            return raw[7:].strip() if raw.lower().startswith("bearer ") else raw.strip()
    return None


class ScopedKnowledgeMCP:
    """ASGI wrapper: authenticate, inject the whole-library or single-source scope, then delegate to the MCP app."""

    def __init__(self, parent_app: FastAPI, mcp_asgi) -> None:
        self._parent = parent_app
        self._mcp = mcp_asgi

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self._mcp(scope, receive, send)
            return

        params = parse_qs((scope.get("query_string") or b"").decode())
        source_id = (params.get("source_id") or [""])[0].strip()

        token = _bearer(scope)
        if not token:
            await _send_json(send, 401, {"error": "Missing authentication token"})
            return
        try:
            decode_token(token)
        except jwt.PyJWTError:
            await _send_json(send, 401, {"error": "Token is invalid or expired"})
            return

        async with SessionLocal() as session:
            statement = select(Source).order_by(Source.created_at, Source.id)
            if source_id:
                statement = statement.where(Source.id == source_id)
            sources = tuple((await session.execute(statement)).scalars().all())
        if source_id and not sources:
            await _send_json(send, 404, {"error": "Source does not exist"})
            return

        engine_manager = self._parent.state.engine_manager
        with use_scope(
            engine_manager,
            sources,
            actor=_actor(scope, params),
            transport="http",
        ):
            await self._mcp(scope, receive, send)


def attach_source_mcp(app: FastAPI) -> FastMCP:
    """Build the HTTP knowledge-base MCP and mount it at `/mcp`.

    The inner FastMCP routes at the root `/` and the outer layer takes over with `Mount("/mcp")` - that avoids a
    doubled `/mcp` inside `/mcp`. External hosts use `/mcp/` with the trailing slash; `source_id` only drives the optional single-source compatibility mode.
    """
    # FastMCP by default reads host=127.0.0.1 as "only accept a localhost Host header".
    # This ASGI app actually sits under a FastAPI reachable over the LAN or a reverse proxy, and the outer
    # layer enforces Bearer authentication, so the SDK's localhost-only Host allowlist is turned off.
    mcp = build_source_mcp(
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    mcp.settings.streamable_http_path = "/"
    mcp_asgi = mcp.streamable_http_app()  # lazily creates session_manager
    app.mount("/mcp", ScopedKnowledgeMCP(app, mcp_asgi))
    return mcp
