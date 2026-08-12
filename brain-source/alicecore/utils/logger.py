"""
Logging utilities

Provides structured logging
"""

import logging
import sys
from typing import Any, Dict, Optional

from alicecore.core.config import get_settings


def setup_logging(
    level: Optional[str] = None,
    format_type: Optional[str] = None,
) -> logging.Logger:
    """
    Configure the logging system

    Args:
        level: log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        format_type: log format (json, text)

    Returns:
        The root logger
    """
    settings = get_settings()
    log_level = level or settings.log_level
    log_format = format_type or settings.log_format

    # Create the root logger
    logger = logging.getLogger("pipeline")
    logger.setLevel(getattr(logging, log_level))

    # Clear the existing handlers
    logger.handlers.clear()

    # Create the console handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(getattr(logging, log_level))

    # Set the format
    if log_format == "json":
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


class JsonFormatter(logging.Formatter):
    """JSON formatter"""

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record as JSON"""
        import json
        from datetime import datetime

        log_data: Dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add the extra fields
        if hasattr(record, "extra"):
            log_data.update(record.extra)

        # Add the exception information
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data, ensure_ascii=False)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger

    Args:
        name: logger name

    Returns:
        A logger instance
    """
    return logging.getLogger(f"alicecore.{name}")


# Default logger
logger = get_logger("main")
