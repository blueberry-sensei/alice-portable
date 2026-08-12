"""Backend adapter interfaces (Protocol).

Structural interfaces: an existing client satisfies them as soon as its methods match, which makes wrapping the current
implementations as default adapters easy, and makes injecting a fake in tests easy too. Storage adapters additionally declare `capabilities` and `provider`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from alicecore.core.adapters.capabilities import Capability


@runtime_checkable
class LLMAdapter(Protocol):
    """LLM adapter: conversation + structured output."""

    async def chat(self, messages: Sequence[Any], **kwargs: Any) -> Any: ...

    async def chat_with_schema(
        self, messages: Sequence[Any], response_schema: dict[str, Any], **kwargs: Any
    ) -> Any: ...


@runtime_checkable
class EmbeddingAdapter(Protocol):
    """Embedding adapter: single and batch."""

    async def generate(self, text: str) -> list[float]: ...

    async def batch_generate(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class RerankAdapter(Protocol):
    """Rerank adapter."""

    async def rerank(
        self,
        query: str,
        documents: list[dict[str, str]],
        top_n: int | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class VectorStore(Protocol):
    """Vector store adapter handle (the method surface lands with M2)."""

    provider: str
    capabilities: frozenset[Capability]

    def raw(self) -> Any:
        """Return the underlying client (for core code not yet migrated during the transition)."""
        ...

    async def ping(self, timeout: float = ...) -> None:
        """Connectivity check; raises on failure."""
        ...


@runtime_checkable
class RelationalStore(Protocol):
    """Relational store adapter handle (the method surface lands with M2)."""

    provider: str
    capabilities: frozenset[Capability]

    def session_factory(self) -> Any:
        """Return the async_sessionmaker so repositories and the engine can take a session."""
        ...

    async def ping(self, timeout: float = ...) -> None:
        """Connectivity check; raises on failure."""
        ...
