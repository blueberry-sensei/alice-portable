"""Lightweight logging setup + request tracing middleware."""

from __future__ import annotations

import contextvars
import logging
import sys
import uuid
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

_CONFIGURED = False

# Trace id of the current request, referenced by logs and error handling
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def _file_handler() -> logging.Handler | None:
    """Handler ghi log ra file, xoay vòng theo dung lượng.

    Log của `alicecore` cũng vào đây: logger của engine propagate lên root, nên một handler
    ở root là đủ để có cả API và engine trong **một** file — khi lỗi thì chỉ cần đọc một chỗ.

    Không tạo được file (chỉ đọc / hết đĩa) thì trả `None`: mất log ra file còn chấp nhận được,
    chứ không được vì chuyện đó mà app không chạy.
    """
    from logging.handlers import RotatingFileHandler

    from sag_api.core.config import settings

    try:
        directory = Path(settings.log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            directory / "sag-api.log",
            maxBytes=settings.log_file_max_mb * 1024 * 1024,
            backupCount=settings.log_file_backups,
            encoding="utf-8",
        )
    except OSError as error:
        print(f"[log] Không ghi được log ra file ({error}); chỉ còn stdout", file=sys.stderr)
        return None

    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(
        logging.Formatter(
            # File log dùng ngày đầy đủ: đọc lại sau vài ngày mà chỉ có giờ thì vô dụng.
            fmt="%(asctime)s  %(levelname)-7s  [%(request_id)s]  %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    return handler


def configure_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s  %(levelname)-7s  [%(request_id)s]  %(name)s  %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]
    file_handler = _file_handler()
    if file_handler is not None:
        root.addHandler(file_handler)
    # Turn down third-party noise, and stop model clients from dumping full prompts/bodies in DEBUG mode.
    for noisy in (
        "httpx",
        "httpcore",
        "openai",
        "lancedb",
        "aiosqlite",
        "LiteLLM",
        "LiteLLM Router",
        "LiteLLM Proxy",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("alicecore.ai.openai").setLevel(logging.INFO)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"sag.{name}")


class _RequestIdFilter(logging.Filter):
    """Inject the current request id into every log record; '-' when outside a request context."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a trace id per request: read X-Request-Id inbound or mint one, and write it back on the response."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        request.state.request_id = rid
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-Id"] = rid
        return response
