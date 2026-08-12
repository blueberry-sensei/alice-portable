"""Adapter capability model.

Backends differ in what they support (ES has native BM25, pgvector needs pg_trgm/tsvector, a numpy fallback has no lexical search).
Every storage adapter declares the `Capability` set it supports, and the engine uses that **at configuration/startup time** to validate
"strategy x backend" compatibility and either fail fast or pick a fallback, so nothing fails silently at runtime.
"""

from __future__ import annotations

from enum import StrEnum


class Capability(StrEnum):
    """Backend capability flag."""

    VECTOR_KNN = "vector_knn"  # vector nearest-neighbour search
    FILTERED_KNN = "filtered_knn"  # vector search with a filter
    LEXICAL_SEARCH = "lexical_search"  # lexical / BM25 search (MULTI_ES depends on it)
    UPSERT = "upsert"  # idempotent write (ON CONFLICT / ON DUPLICATE KEY)


class MissingCapabilityError(Exception):
    """The required capability is unavailable on the current backend (raised by `require_capability`)."""


def has_capability(obj: object, capability: Capability) -> bool:
    """Whether the adapter declares support for a capability."""
    caps: frozenset[Capability] = getattr(obj, "capabilities", frozenset())
    return capability in caps


def require_capability(obj: object, capability: Capability, *, context: str = "") -> None:
    """Assert the adapter supports a capability, otherwise raise with a clear message."""
    if not has_capability(obj, capability):
        where = f"({context}) " if context else ""
        provider = getattr(obj, "provider", type(obj).__name__)
        raise MissingCapabilityError(
            f"{where}backend '{provider}' does not support the capability '{capability.value}'; "
            "switch backend, or use a strategy that does not need it."
        )
