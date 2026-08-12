"""Storage module.

The vector backend client plus the repository access layer. **The default path never imports elasticsearch**:
the vector client is resolved by the provider-neutral ``client.get_vector_client`` (lancedb / pgvector / es),
and only the two ES-specific symbols ``ESConfig`` / ``ElasticsearchClient`` are exported lazily (PEP 562),
so ``import alicecore.core.storage`` never pulls in the elasticsearch package (ES is an optional extra).
"""

from __future__ import annotations

from typing import Any

from alicecore.core.storage.client import (
    close_vector_client,
    get_vector_client,
    reset_vector_client,
)
from alicecore.core.storage.repositories import (
    BaseRepository,
    EntityVectorRepository,
    EventVectorRepository,
    SourceChunkRepository,
)

# Backwards compatible aliases
get_es_client = get_vector_client
reset_es_client = reset_vector_client
close_es_client = close_vector_client

__all__ = [
    # Vector client (provider neutral)
    "get_vector_client",
    "reset_vector_client",
    "close_vector_client",
    "get_es_client",
    "reset_es_client",
    "close_es_client",
    # ES specific (lazy, needs the [es] extra)
    "ESConfig",
    "ElasticsearchClient",
    # Repositories
    "BaseRepository",
    "EntityVectorRepository",
    "EventVectorRepository",
    "SourceChunkRepository",
]


def __getattr__(name: str) -> Any:
    """Lazily export the ES-specific symbols (elasticsearch is imported only on real access)."""
    if name in ("ESConfig", "ElasticsearchClient"):
        from alicecore.core.storage import elasticsearch as _es

        return getattr(_es, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
