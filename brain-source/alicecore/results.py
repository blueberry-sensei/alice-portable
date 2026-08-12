"""Result models the facade layer returns to callers.

Deliberately decoupled from the core's internal types: the internal objects the core returns are converted here into
simple models, so a user only depends on a stable public shape and is unaffected by core implementation changes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class IngestResult(BaseModel):
    """Result of the ingest stage."""

    source_config_id: str
    source_id: str | None = None
    chunk_count: int = 0
    chunk_ids: list[str] = Field(default_factory=list)


class ExtractResult(BaseModel):
    """Result of the extract stage."""

    source_config_id: str
    event_count: int = 0
    event_ids: list[str] = Field(default_factory=list)


class SearchResult(BaseModel):
    """Result of the search stage."""

    query: str
    sections: list[dict[str, Any]] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)


class ChunkItem(BaseModel):
    """One in-memory chunk (not persisted)."""

    heading: str = ""
    content: str = ""
    raw_content: str | None = None
    rank: int = 0
    chunk_type: str | None = None


class ChunkResult(BaseModel):
    """Result of the chunk stage (split only, nothing stored)."""

    chunk_count: int = 0
    chunks: list[ChunkItem] = Field(default_factory=list)
