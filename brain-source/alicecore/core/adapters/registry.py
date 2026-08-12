"""Adapter factory + provider registry (mirroring cognee's create_*_engine).

Picks an implementation by `provider` name and instantiates it; `register` may inject a custom or test adapter at runtime.
The default providers form the zero-infrastructure local stack (llm/embedding/rerank=openai, vector=lancedb,
relational=sqlite), so it runs locally with no service and no explicit configuration.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from alicecore.core.adapters import defaults
from alicecore.core.adapters.capabilities import Capability
from alicecore.exceptions import ConfigError

# kind -> {provider -> factory(**kwargs) -> adapter}
_PROVIDERS: dict[str, dict[str, Callable[..., Any]]] = {
    "llm": {"openai": defaults.OpenAILLMAdapter},
    "embedding": {"openai": defaults.OpenAIEmbeddingAdapter},
    "rerank": {"openai": defaults.HTTPRerankAdapter},
    "vector": {
        "es": defaults.ESVectorStore,
        "lancedb": defaults.LanceDBVectorStore,
        "pgvector": defaults.PgVectorStoreAdapter,
        "oceanbase": defaults.OceanBaseVectorStoreAdapter,
    },
    "relational": {
        "mysql": lambda **kw: defaults.SqlAlchemyRelationalStore(provider="mysql", **kw),
        "postgres": lambda **kw: defaults.SqlAlchemyRelationalStore(provider="postgres", **kw),
        "sqlite": lambda **kw: defaults.SqlAlchemyRelationalStore(provider="sqlite", **kw),
        "oceanbase": lambda **kw: defaults.SqlAlchemyRelationalStore(provider="oceanbase", **kw),
    },
}

# The default providers when nothing is configured (= the zero-infrastructure local stack)
_DEFAULTS: dict[str, str] = {
    "llm": "openai",
    "embedding": "openai",
    "rerank": "openai",
    "vector": "lancedb",
    "relational": "sqlite",
}


def register(kind: str, provider: str, factory: Callable[..., Any]) -> None:
    """Register (or replace) a provider implementation; used to extend backends or inject one in tests."""
    if kind not in _PROVIDERS:
        raise ConfigError(f"Unknown adapter category '{kind}'; choose from: {', '.join(_PROVIDERS)}")
    _PROVIDERS[kind][provider] = factory


def create(kind: str, provider: str | None = None, **kwargs: Any) -> Any:
    """Create an adapter instance by category + provider. An empty provider uses the default."""
    if kind not in _PROVIDERS:
        raise ConfigError(f"Unknown adapter category '{kind}'; choose from: {', '.join(_PROVIDERS)}")
    name = provider or _DEFAULTS[kind]
    impls = _PROVIDERS[kind]
    if name not in impls:
        raise ConfigError(f"Adapter '{kind}' has no provider '{name}'; registered: {', '.join(impls)}")
    return impls[name](**kwargs)


def create_llm(provider: str | None = None, **kwargs: Any) -> Any:
    return create("llm", provider, **kwargs)


def create_embedding(provider: str | None = None, **kwargs: Any) -> Any:
    return create("embedding", provider, **kwargs)


def create_rerank(provider: str | None = None, **kwargs: Any) -> Any:
    return create("rerank", provider, **kwargs)


def create_vector_store(provider: str | None = None, **kwargs: Any) -> Any:
    return create("vector", provider, **kwargs)


def create_relational_store(provider: str | None = None, **kwargs: Any) -> Any:
    return create("relational", provider, **kwargs)


def capabilities(kind: str, provider: str | None = None, **kwargs: Any) -> frozenset[Capability]:
    """Query the capability set a backend declares (storage adapters)."""
    inst = create(kind, provider, **kwargs)
    return getattr(inst, "capabilities", frozenset())


def available_providers(kind: str) -> list[str]:
    """List the registered provider names of a category."""
    if kind not in _PROVIDERS:
        raise ConfigError(f"Unknown adapter category '{kind}'; choose from: {', '.join(_PROVIDERS)}")
    return sorted(_PROVIDERS[kind])
