"""Resource container and scope resolution (the single source of truth for DI).

`ResourceContainer` holds every backend adapter one engine needs (llm/embedding/rerank/
vector/relational), built from `EngineConfig` through the registry. `get_resources()` resolves in one place:
use the container the context set, otherwise return the **default container** (= the current production stack mysql/es/openai) -
there is **no second path that falls back to a bare global singleton**, so a ContextVar that does not propagate cannot silently connect to the wrong backend.

Transition note: the current default adapters still reuse process-level globals underneath (get_engine/get_es_client and so on),
so behaviour matches production today (zero regression); real multi-configuration isolation comes later, when each adapter owns its connection.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from alicecore.core.adapters import registry

if TYPE_CHECKING:
    from alicecore.core.adapters import (
        EmbeddingAdapter,
        LLMAdapter,
        RelationalStore,
        RerankAdapter,
        VectorStore,
    )


@dataclass(frozen=True)
class ResourceContainer:
    """Every backend adapter one engine, or one execution, needs."""

    llm: LLMAdapter
    embedding: EmbeddingAdapter
    rerank: RerankAdapter
    vector: VectorStore
    relational: RelationalStore


def _provider(config: Any, kind: str) -> str | None:
    """Read the provider name of a storage category (relational/vector only; the AI side always uses the default adapter).

    Note: ``config.llm.provider`` (openai/litellm) chooses the **client implementation** and is handled by the factory through
    the ``LLM_PROVIDER`` environment variable - it is not an adapter provider, so the AI-side provider is not read here.
    """
    if kind not in ("relational", "vector"):
        return None
    if kind == "vector":
        # EngineConfig's vector choice is the string field vector_provider (there is no nested config object);
        # a vector.provider object on a custom config is also accepted.
        direct = getattr(config, "vector_provider", None)
        if direct:
            return str(direct)
    section = getattr(config, kind, None)
    return getattr(section, "provider", None)


def build_container(config: Any = None) -> ResourceContainer:
    """Build the resource container from the configuration; an unspecified provider uses the default (production stack)."""
    return ResourceContainer(
        llm=registry.create_llm(_provider(config, "llm")),
        embedding=registry.create_embedding(_provider(config, "embedding")),
        rerank=registry.create_rerank(_provider(config, "rerank")),
        vector=registry.create_vector_store(_provider(config, "vector")),
        relational=registry.create_relational_store(_provider(config, "relational")),
    )


_current: ContextVar[ResourceContainer | None] = ContextVar("sag_resources", default=None)
_default_container: ResourceContainer | None = None


def get_resources() -> ResourceContainer:
    """Resolve the resource container in effect (the context wins, otherwise the default container)."""
    current = _current.get()
    if current is not None:
        return current
    global _default_container
    if _default_container is None:
        _default_container = build_container()
    return _default_container


def set_resources(container: ResourceContainer) -> Any:
    """Make the container the one in effect for the current context; returns a token usable for reset."""
    return _current.set(container)


def reset_resources(token: Any) -> None:
    """Restore the context to what it was before set."""
    _current.reset(token)
