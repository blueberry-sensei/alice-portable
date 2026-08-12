"""Default adapter implementations.

Wraps the existing clients (OpenAI LLM/Embedding, HTTP Rerank, ES, SQLAlchemy) as adapters.
All of them are **lazy**: construction performs no I/O and the underlying client is created on first use, so the registry's
selection process has no side effects and is unit-testable. Behaviour matches the status quo (zero regression).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from alicecore.core.adapters.capabilities import Capability


class OpenAILLMAdapter:
    """Wraps `core.ai.factory.create_llm_client` (OpenAI compatible).

    Note: M1 keeps the existing implementation; litellm + instructor plug into the same interface later as a replacement.
    """

    provider = "openai"

    def __init__(
        self, scenario: str = "general", model_config: dict[str, Any] | None = None
    ) -> None:
        self._scenario = scenario
        self._model_config = model_config
        self._client: Any = None

    async def _get(self) -> Any:
        if self._client is None:
            from alicecore.core.ai.factory import create_llm_client

            self._client = await create_llm_client(self._scenario, self._model_config)
        return self._client

    async def chat(self, messages: Sequence[Any], **kwargs: Any) -> Any:
        return await (await self._get()).chat(messages, **kwargs)

    async def chat_with_schema(
        self, messages: Sequence[Any], response_schema: dict[str, Any], **kwargs: Any
    ) -> Any:
        return await (await self._get()).chat_with_schema(
            messages, response_schema=response_schema, **kwargs
        )


class OpenAIEmbeddingAdapter:
    """Wraps `core.ai.factory.get_embedding_client`."""

    provider = "openai"

    def __init__(self, scenario: str = "general") -> None:
        self._scenario = scenario
        self._client: Any = None

    async def _get(self) -> Any:
        if self._client is None:
            from alicecore.core.ai.factory import get_embedding_client

            self._client = await get_embedding_client(self._scenario)
        return self._client

    async def generate(self, text: str) -> list[float]:
        return await (await self._get()).generate(text)

    async def batch_generate(self, texts: list[str]) -> list[list[float]]:
        return await (await self._get()).batch_generate(texts)


class HTTPRerankAdapter:
    """Wraps `core.ai.rerank.get_rerank_client`."""

    provider = "openai"

    def __init__(self) -> None:
        self._client: Any = None

    def _get(self) -> Any:
        if self._client is None:
            from alicecore.core.ai.rerank import get_rerank_client

            self._client = get_rerank_client()
        return self._client

    async def rerank(
        self,
        query: str,
        documents: list[dict[str, str]],
        top_n: int | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        return await self._get().rerank(query, documents, top_n=top_n, **kwargs)


class ESVectorStore:
    """Wraps `core.storage.elasticsearch.get_es_client`."""

    provider = "es"
    capabilities = frozenset(
        {Capability.VECTOR_KNN, Capability.FILTERED_KNN, Capability.LEXICAL_SEARCH}
    )

    def __init__(self) -> None:
        self._client: Any = None

    def raw(self) -> Any:
        if self._client is None:
            from alicecore.core.storage.client import get_es_client

            self._client = get_es_client()
        return self._client

    async def ping(self, timeout: float = 5.0) -> None:
        """Connectivity check: raises StorageError (with a clear message) on failure."""
        import asyncio

        from alicecore.exceptions import StorageError

        client = self.raw()  # construction / first import is outside the timeout window

        async def _check() -> None:
            ok = await client.ping()
            if not ok:
                raise RuntimeError("ping returned False")

        try:
            await asyncio.wait_for(_check(), timeout=timeout)
        except Exception as e:
            raise StorageError(f"Elasticsearch connection failed: {e}") from e


class LanceDBVectorStore:
    """Wraps the LanceDB local embedded backend (dispatched at runtime through `get_es_client`).

    It ships tantivy BM25 full-text search, so it declares LEXICAL_SEARCH and the multi_es strategy works locally.
    """

    provider = "lancedb"
    capabilities = frozenset(
        {Capability.VECTOR_KNN, Capability.FILTERED_KNN, Capability.LEXICAL_SEARCH}
    )

    def __init__(self) -> None:
        self._client: Any = None

    def raw(self) -> Any:
        if self._client is None:
            from alicecore.core.storage.client import get_es_client

            self._client = get_es_client()
        return self._client

    async def ping(self, timeout: float = 5.0) -> None:
        """Connectivity check: raises StorageError (with a clear message) on failure."""
        import asyncio

        from alicecore.exceptions import StorageError

        client = self.raw()  # construction / first import (the lancedb native library cold load) is outside the timeout window

        async def _check() -> None:
            ok = await client.ping()
            if not ok:
                raise RuntimeError("ping returned False")

        try:
            await asyncio.wait_for(_check(), timeout=timeout)
        except Exception as e:
            raise StorageError(f"LanceDB connection failed: {e}") from e


class PgVectorStoreAdapter:
    """Wraps the pgvector backend (reuses the postgres relational database, dispatched at runtime through `get_es_client`).

    Note: pgvector's full-text search degrades to ILIKE rather than real BM25, so it does not declare LEXICAL_SEARCH;
    the multi_es strategy is rejected explicitly by the capability check on this backend.
    """

    provider = "pgvector"
    capabilities = frozenset({Capability.VECTOR_KNN, Capability.FILTERED_KNN})

    def __init__(self) -> None:
        self._client: Any = None

    def raw(self) -> Any:
        if self._client is None:
            from alicecore.core.storage.client import get_es_client

            self._client = get_es_client()
        return self._client

    async def ping(self, timeout: float = 5.0) -> None:
        """Connectivity check: raises StorageError (with a clear message) on failure."""
        import asyncio

        from alicecore.exceptions import StorageError

        client = self.raw()  # construction / first import is outside the timeout window

        async def _check() -> None:
            ok = await client.ping()
            if not ok:
                raise RuntimeError("ping returned False")

        try:
            await asyncio.wait_for(_check(), timeout=timeout)
        except Exception as e:
            raise StorageError(f"pgvector connection failed: {e}") from e


class OceanBaseVectorStoreAdapter:
    """Wraps the OceanBase vector backend (reuses the OB relational database, one database for SQL and vectors, dispatched at runtime through `get_es_client`).

    OceanBase vector full-text search degrades to LIKE rather than real BM25, so it does not declare LEXICAL_SEARCH;
    the multi_es strategy is rejected explicitly by the capability check on this backend (same as pgvector).
    """

    provider = "oceanbase"
    capabilities = frozenset({Capability.VECTOR_KNN, Capability.FILTERED_KNN})

    def __init__(self) -> None:
        self._client: Any = None

    def raw(self) -> Any:
        if self._client is None:
            from alicecore.core.storage.client import get_es_client

            self._client = get_es_client()
        return self._client

    async def ping(self, timeout: float = 5.0) -> None:
        """Connectivity check: raises StorageError (with a clear message) on failure."""
        import asyncio

        from alicecore.exceptions import StorageError

        client = self.raw()  # construction / first import is outside the timeout window

        async def _check() -> None:
            ok = await client.ping()
            if not ok:
                raise RuntimeError("ping returned False")

        try:
            await asyncio.wait_for(_check(), timeout=timeout)
        except Exception as e:
            raise StorageError(f"OceanBase connection failed: {e}") from e


class SqlAlchemyRelationalStore:
    """Wraps the async engine / session factory of `db.base`, telling providers apart by dialect (mysql/postgres/sqlite)."""

    capabilities = frozenset({Capability.UPSERT})

    def __init__(self, provider: str = "mysql") -> None:
        self.provider = provider

    def session_factory(self) -> Any:
        from alicecore.db.base import get_session_factory

        return get_session_factory()

    async def ping(self, timeout: float = 5.0) -> None:
        """Connectivity check (SELECT 1): raises StorageError (with a clear message) on failure."""
        import asyncio

        from sqlalchemy import text

        from alicecore.exceptions import StorageError

        async def _check() -> None:
            factory = self.session_factory()
            async with factory() as session:
                await session.execute(text("SELECT 1"))

        try:
            await asyncio.wait_for(_check(), timeout=timeout)
        except Exception as e:
            raise StorageError(f"Relational connection failed ({self.provider}): {e}") from e
