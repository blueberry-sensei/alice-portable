"""Backend adapter layer.

Defines the adapter interfaces for LLM/Embedding/Rerank/VectorStore/RelationalStore, plus a capability model and a
factory/registry keyed by provider name (mirroring cognee). The default implementations wrap the existing clients, with unchanged behaviour.

Usage::

    from alicecore.core.adapters import registry
    emb = registry.create_embedding()            # openai by default
    vec = registry.create_vector_store("es")      # pick a provider explicitly
    caps = registry.capabilities("vector", "es")  # query the capabilities
"""

from alicecore.core.adapters.capabilities import (
    Capability,
    MissingCapabilityError,
    has_capability,
    require_capability,
)
from alicecore.core.adapters.interfaces import (
    EmbeddingAdapter,
    LLMAdapter,
    RelationalStore,
    RerankAdapter,
    VectorStore,
)

__all__ = [
    "Capability",
    "MissingCapabilityError",
    "has_capability",
    "require_capability",
    "LLMAdapter",
    "EmbeddingAdapter",
    "RerankAdapter",
    "VectorStore",
    "RelationalStore",
]
