"""
Search module

Provides the SAG search engine with five strategies: VECTOR / ATOMIC / MULTI / MULTI1 / HOPLLM

Architecture:
- SAGSearcher/EventSearcher: the unified search entry point (recommended)
- VectorSearcher: pure vector retriever
- AtomicSearcher: atomic event retriever
- MultiSearcher: multi-element event retriever (supports the multi/multi1/hopllm strategies)
"""

from alicecore.modules.search.config import (
    SearchConfig,
    SearchBaseConfig,
    RerankConfig,
    VectorConfig,
    AtomicConfig,
    MultiConfig,
    RerankStrategy,
)
from alicecore.modules.search.searcher import (
    SAGSearcher,
    EventSearcher,
)
from alicecore.modules.search.vector import VectorSearcher
from alicecore.modules.search.atomic import AtomicSearcher
from alicecore.modules.search.multi import MultiSearcher
from alicecore.modules.search.multi_vector import ESFirstMultiSearcher

__all__ = [
    # Configuration
    "SearchConfig",
    "SearchBaseConfig",
    "RerankConfig",
    "VectorConfig",
    "AtomicConfig",
    "MultiConfig",
    "RerankStrategy",
    # Searchers (recommended)
    "SAGSearcher",
    "EventSearcher",
    "VectorSearcher",
    "AtomicSearcher",
    "MultiSearcher",
    "ESFirstMultiSearcher",
]
