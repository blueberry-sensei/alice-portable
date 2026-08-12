"""Connector abstraction - the pluggable interface of the ingestion layer.

A "connector" turns an external information source into local files the engine can process:

- **static** (such as file upload): the user pushes documents directly, `supports_sync=False`.
- **dynamic** (such as Web / Notion / S3, to be added): implement `discover()` to enumerate remote documents and
  `fetch()` to pull them locally; the `sync_source` task calls them periodically.

Adding a connector = subclass `Connector`, implement the methods, register it in `registry` - no upper-layer change needed.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from typing import Any

from sag_api.enums import ConnectorKind


@dataclass
class ConfigField:
    """Description of a connector configuration field (so the frontend can render the form dynamically)."""

    key: str
    label: str
    type: str = "string"  # string | password | number | boolean | url
    required: bool = False
    placeholder: str = ""
    help: str = ""


@dataclass
class ConnectorMeta:
    kind: ConnectorKind
    title: str
    description: str
    supports_sync: bool = False
    config_fields: list[ConfigField] = field(default_factory=list)

    def to_public(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "title": self.title,
            "description": self.description,
            "supports_sync": self.supports_sync,
            "config_fields": [f.__dict__ for f in self.config_fields],
        }


@dataclass
class DiscoveredDoc:
    """A remote document discovered by a dynamic connector."""

    external_id: str
    filename: str
    content_type: str = "application/octet-stream"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class LocalFile:
    """A file already stored locally, ready for the engine to ingest."""

    path: str
    filename: str
    content_type: str
    size_bytes: int


class Connector(ABC):
    """Base class of every connector."""

    meta: ConnectorMeta

    def validate_config(self, config: dict[str, Any]) -> None:
        """Validate the source configuration; raise `ValidationError` when it is invalid. Required fields are checked by default."""
        from sag_api.core.errors import ValidationError

        for f in self.meta.config_fields:
            if f.required and not (config or {}).get(f.key):
                raise ValidationError(f"Missing required configuration field: {f.label} ({f.key})")

    async def discover(self, config: dict[str, Any]) -> list[DiscoveredDoc]:
        """Enumerate remote documents (implemented by dynamic connectors)."""
        raise NotImplementedError

    async def fetch(self, config: dict[str, Any], doc: DiscoveredDoc) -> LocalFile:
        """Pull one remote document to local storage (implemented by dynamic connectors)."""
        raise NotImplementedError
