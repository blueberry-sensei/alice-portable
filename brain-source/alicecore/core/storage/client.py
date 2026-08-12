"""Vector backend client (a provider-neutral singleton).

Dispatches to the lancedb / pgvector / es backend from ``settings.vector_provider`` and caches it as a process-level singleton.
**The point**: this module never imports elasticsearch itself; only the provider=es branch lazily imports
`elasticsearch.py`. So on the default (lancedb) path, the whole import chain of ``DataEngine.start()``
never touches the elasticsearch package - which is what lets ES be an optional extra.

``get_es_client`` / ``reset_es_client`` / ``close_es_client`` remain as backwards compatible aliases.
"""

from __future__ import annotations

from typing import Any

from alicecore.core.config import get_settings
from alicecore.exceptions import StorageError
from alicecore.utils import get_logger

logger = get_logger("storage.client")

_client: Any | None = None


def get_vector_client() -> Any:
    """Get the vector backend client singleton (method surface matching ElasticsearchClient, shared by the repositories)."""
    global _client
    if _client is None:
        settings = get_settings()
        provider = (getattr(settings, "vector_provider", "lancedb") or "lancedb").lower()
        if provider == "lancedb":
            from alicecore.core.storage.lancedb_store import LanceDBStore

            _client = LanceDBStore(settings.lancedb_uri)
            logger.info(f"Vector backend: lancedb (local embedded, {settings.lancedb_uri})")
        elif provider == "pgvector":
            if (settings.db_provider or "").lower() not in ("postgres", "postgresql"):
                raise StorageError("vector_provider=pgvector needs db_provider=postgres (it reuses the same PG database)")
            from alicecore.core.storage.pgvector_store import PgVectorStore

            _client = PgVectorStore()
            logger.info("Vector backend: pgvector (reusing the PostgreSQL relational database)")
        elif provider == "oceanbase":
            if (settings.db_provider or "").lower() != "oceanbase":
                raise StorageError("vector_provider=oceanbase needs db_provider=oceanbase (it reuses the same OB database)")
            from alicecore.core.storage.oceanbase_store import OceanBaseVectorStore

            _client = OceanBaseVectorStore()
            logger.info("Vector backend: oceanbase (reusing the OceanBase relational database, one database for SQL and vectors)")
        else:  # es - only this branch lazily imports elasticsearch
            from alicecore.core.storage.elasticsearch import ElasticsearchClient

            _client = ElasticsearchClient()
            logger.info("Vector backend: elasticsearch")
    return _client


def reset_vector_client() -> None:
    """Reset the singleton (without closing the connection); used when a Celery worker changes event loop or the configuration changes."""
    global _client
    _client = None


async def close_vector_client() -> None:
    """Close and reset the singleton."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None


# -- Backwards compatible aliases (the historical call surface) --------
get_es_client = get_vector_client
reset_es_client = reset_vector_client
close_es_client = close_vector_client
