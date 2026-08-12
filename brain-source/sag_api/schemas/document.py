from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sag_api.enums import DocumentStatus


class MessageItem(BaseModel):
    text: str
    author: str | None = None
    role: str | None = None
    ts: str | None = None
    thread: str | None = None


class IngestRequest(BaseModel):
    """Unified write: either a text or a batch of messages, one of the two."""

    text: str | None = None
    title: str | None = None
    messages: list[MessageItem] | None = Field(default=None)


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source_id: str
    filename: str
    content_type: str
    size_bytes: int
    status: DocumentStatus
    chunk_count: int
    event_count: int
    progress: int
    token_usage: int
    error: str | None
    created_at: datetime
    updated_at: datetime

    @field_validator("error", mode="before")
    @classmethod
    def redact_storage_error(cls, value: object) -> object:
        """Keep SQL and local storage details in server logs, not in the UI."""
        if not isinstance(value, str):
            return value
        normalized = value.lower()
        sql_markers = (
            "sqlite3.",
            "sqlalchemy.exc",
            "[sql:",
            "[parameters:",
            "sqlalche.me/e/",
        )
        if not any(marker in normalized for marker in sql_markers):
            return value
        if "foreign key constraint failed" in normalized:
            return "The source has not finished initialising, so the document is not stored yet; please retry."
        return "Storing the document failed; please retry, and check the service log if it keeps failing."
