"""Enums shared across layers (models / schemas / services may all import them, no side effects)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

SearchStrategy = Literal["vector", "multi"]
SEARCH_STRATEGIES = frozenset({"vector", "multi"})


def normalize_search_strategy(value: str) -> str:
    """Migrate the retired atomic retrieval to precise retrieval; other values are left to the caller to validate."""
    return "multi" if value == "atomic" else value


class SourceType(StrEnum):
    DOCUMENT = "document"
    WEB = "web"
    MESSAGE = "message"
    AUDIO = "audio"


class ConnectorKind(StrEnum):
    FILE_UPLOAD = "file_upload"
    WEB = "web"
    # Reserved: NOTION = "notion"; S3 = "s3"; CONFLUENCE = "confluence"; ...


# Connector -> default source type
CONNECTOR_SOURCE_TYPE = {
    ConnectorKind.FILE_UPLOAD: SourceType.DOCUMENT,
    ConnectorKind.WEB: SourceType.WEB,
}


class SourceStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"


class DocumentStatus(StrEnum):
    PENDING = "pending"        # registered, awaiting processing
    LOADING = "loading"        # in ingest (parse -> chunk -> store -> vectorise)
    EXTRACTING = "extracting"  # in extract (event / entity extraction)
    PAUSED = "paused"          # extraction paused, resumable from the chunk checkpoint
    READY = "ready"            # processing finished, searchable
    FAILED = "failed"


class JobType(StrEnum):
    PROCESS_DOCUMENT = "process_document"
    SYNC_SOURCE = "sync_source"
    INDEX_UNIVERSE = "index_universe"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class BindingTargetType(StrEnum):
    SOURCE = "source"
    MCP_SERVER = "mcp_server"  # Phase C: mount an MCP server as a tool source
