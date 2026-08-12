"""sag-api application entry point."""

from __future__ import annotations

import asyncio
import os
from contextlib import AsyncExitStack, asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from sag_agent import AgentRuntime
from sag_api import __version__
from sag_api.api.v1 import api_router
from sag_api.branding import PRODUCT_NAME
from sag_api.core.config import settings
from sag_api.core.db import SessionLocal, dispose_db, init_db
from sag_api.core.errors import ApiError
from sag_api.core.litellm_policy import install_litellm_policy, uninstall_litellm_policy
from sag_api.core.logging import RequestContextMiddleware, configure_logging, get_logger
from sag_api.core.telemetry_litellm import install_litellm_telemetry, uninstall_litellm_telemetry
from sag_api.generation import LLMClient
from sag_api.jobs import InProcessAsyncQueue
from sag_api.sag import EngineManager
from sag_api.sag.attempt_bridge import install_engine_attempt_bridge, uninstall_engine_attempt_bridge
from sag_api.sag.compat import install_engine_extract_compat
from sag_api.sag.embedding_telemetry import (
    install_engine_embedding_telemetry,
    uninstall_engine_embedding_telemetry,
)
from sag_api.services import telemetry_service
from sag_api.services.telemetry_service import install_telemetry_store, uninstall_telemetry_store

log = get_logger("app")


# Known insecure default secret (production refuses to start)
_INSECURE_SECRETS = {
    "dev-insecure-secret-change-me-in-production-0123456789",
    "please-change-this-in-production-0123456789",
    "dev-secret-change-me",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging("DEBUG" if settings.debug else "INFO")
    if settings.environment == "prod" and settings.secret_key in _INSECURE_SECRETS:
        raise RuntimeError(
            "The default SAG_SECRET_KEY is not allowed in production. Set a strong random value (>=32 bytes), for example: openssl rand -hex 32"
        )
    os.makedirs(settings.data_dir, exist_ok=True)
    os.makedirs(settings.upload_dir, exist_ok=True)

    await init_db()

    # Apply the model configuration stored in the DB onto the settings singleton (before building the LLM/engine)
    from sag_api.services.settings_service import apply_startup_overrides

    await apply_startup_overrides(SessionLocal)

    # Seed the default agent (the out-of-the-box main conversation entry; idempotent)
    from sag_api.services.agent_domain import get_default_agent

    async with SessionLocal() as _session:
        await get_default_agent(_session)

    # alicecore calls LiteLLM internally too; a global pre-call policy makes it share the same provider
    # parameters as the Muse generation chain without patching the dependency.
    install_engine_extract_compat()
    litellm_policy = install_litellm_policy(settings)
    # Telemetry: mọi request LLM (chat lẫn trích xuất) đi qua LiteLLM nên một callback là
    # đủ thấy hết token/chi phí; embedding của engine đi bằng SDK openai nên có sink riêng.
    install_telemetry_store(SessionLocal)
    litellm_telemetry = install_litellm_telemetry()
    install_engine_embedding_telemetry()
    await telemetry_service.prune_now()
    # Engine tự chuyển provider khi trích xuất; hứng log của nó về cùng một chỗ với đường
    # chat để UI chỉ phải đọc một nguồn khi hỏi "vừa rồi provider nào fail".
    install_engine_attempt_bridge()
    app.state.engine_manager = EngineManager(settings)
    app.state.llm = LLMClient(settings)
    app.state.agent_runtime = AgentRuntime()
    await app.state.agent_runtime.start()
    app.state.job_queue = InProcessAsyncQueue(
        SessionLocal, app.state.engine_manager, concurrency=settings.job_concurrency
    )
    await app.state.job_queue.start()

    # Warm up recently used source engines in the background (does not block startup; a failure does not affect service)
    warmup_task = asyncio.create_task(_warmup_engines(app.state.engine_manager))

    log.info(
        "sag-api started - env=%s - llm_configured=%s - vector=%s",
        settings.environment,
        settings.llm_configured,
        settings.sag_vector_provider,
    )
    source_mcp = getattr(app.state, "source_mcp", None)
    try:
        # The MCP endpoint's session manager must run inside the lifespan; a failure only disables /mcp and leaves the rest running
        async with AsyncExitStack() as stack:
            if source_mcp is not None:
                try:
                    await stack.enter_async_context(source_mcp.session_manager.run())
                    log.info("MCP endpoint ready - /mcp/ (whole library) - optional ?source_id=<source id>")
                except Exception as e:  # noqa: BLE001
                    log.warning("MCP session manager failed to start (/mcp unavailable): %s", e)
            yield
    finally:
        try:
            warmup_task.cancel()
            with suppress(asyncio.CancelledError):
                await warmup_task
            await app.state.agent_runtime.stop()
            await app.state.job_queue.stop()
            await app.state.engine_manager.aclose_all()
            await dispose_db()
        finally:
            uninstall_engine_attempt_bridge()
            uninstall_engine_embedding_telemetry()
            uninstall_litellm_telemetry(litellm_telemetry)
            uninstall_telemetry_store()
            uninstall_litellm_policy(litellm_policy)


async def _warmup_engines(engine_manager: EngineManager) -> None:
    """Warm up the most recently updated source engines to shorten the wait on the user's first action."""
    if settings.engine_warmup_count <= 0:
        return
    try:
        from sqlalchemy import select

        from sag_api.db.models import Source

        async with SessionLocal() as session:
            rows = (
                (
                    await session.execute(
                        select(Source).order_by(Source.updated_at.desc()).limit(settings.engine_warmup_count)
                    )
                )
                .scalars()
                .all()
            )
        for source in rows:
            try:
                await engine_manager.provision(source.sag_source_config_id, source)
            except Exception as e:  # noqa: BLE001
                log.warning("Engine warm-up failed source=%s: %s", source.id, e)
        if rows:
            log.info("Warmed up %d source engines", len(rows))
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("Engine warm-up task raised: %s", e)


def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{PRODUCT_NAME} API",
        version=__version__,
        summary="Open-source knowledge base platform - from information sources to knowledge Q&A",
        lifespan=lifespan,
    )

    cors_kwargs: dict = {
        "allow_origins": settings.cors_origins,
        "allow_credentials": True,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
        "expose_headers": ["X-Request-Id"],
    }
    # In development, allow a LAN frontend (for example http://192.168.x.x:3000) so CORS does not block access by machine IP
    if settings.environment == "dev":
        cors_kwargs["allow_origin_regex"] = (
            r"https?://("
            r"localhost|"
            r"127\.0\.0\.1|"
            r"192\.168\.\d{1,3}\.\d{1,3}|"
            r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
            r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
            r")(:\d+)?"
        )
    app.add_middleware(CORSMiddleware, **cors_kwargs)
    # Request tracing (added after CORS -> runs further out, so it assigns request_id first)
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(ApiError)
    async def _handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "Internal server error"}},
        )

    app.include_router(api_router)

    # A source is an MCP endpoint: mount the Streamable-HTTP endpoint (a failure does not block startup)
    try:
        from sag_api.mcp.mount import attach_source_mcp

        app.state.source_mcp = attach_source_mcp(app)
    except Exception as e:  # noqa: BLE001
        app.state.source_mcp = None
        log.warning("Failed to mount the MCP endpoint: %s", e)

    @app.get("/", tags=["system"])
    async def root() -> dict:
        return {"name": PRODUCT_NAME, "version": __version__, "docs": "/docs"}

    return app


app = create_app()
