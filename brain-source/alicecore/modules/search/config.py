"""
Search module configuration

Three search strategies are supported:
- VECTOR: pure vector search
- ATOMIC: atomic event retrieval
- MULTI: multi-element event retrieval (the MultiConfig.strategy parameter selects multi/multi1/hopllm)
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field, field_validator

from alicecore.models.base import pipelineBaseModel


class RerankStrategy(str, Enum):
    """
    Search strategy (three of them)

    - VECTOR:  pure vector matching (skips Recall/Expand and searches sections by vector directly)
    - ATOMIC:  atomic event retrieval (triples + LLM selection)
    - MULTI:   multi-element event retrieval (multi-entity + multi-hop expansion + LLM selection)
               the MultiConfig.strategy parameter selects one of three sub-strategies:
               * "multi": fixed-hop expansion
               * "multi1": two-stage expansion (every seed)
               * "hopllm": two-stage expansion (coarse-ranked seeds)
    """
    VECTOR = "vector"
    ATOMIC = "atomic"
    MULTI = "multi"
    MULTI_ES = "multi_es"

    def __str__(self) -> str:
        return self.value


class ReturnType(str, Enum):
    """
    Return type

    - EVENT: events (default)
    - PARAGRAPH: sections
    """
    EVENT = "event"
    PARAGRAPH = "paragraph"

    def __str__(self) -> str:
        return self.value


class RerankConfig(pipelineBaseModel):
    """
    Search strategy configuration

    Three search modes are supported:
    - VECTOR: pure vector search (skips Recall/Expand)
    - ATOMIC: atomic event retrieval (triples + LLM selection)
    - MULTI: multi-element event retrieval (multi-entity + multi-hop expansion + LLM selection, the default)
            the MultiConfig.strategy parameter selects the multi/multi1/hopllm sub-strategy
    """

    # Ranking strategy
    strategy: RerankStrategy = Field(
        default=RerankStrategy.MULTI,
        description="search strategy (VECTOR/ATOMIC/MULTI)"
    )


class VectorConfig(pipelineBaseModel):
    """
    Vector retriever configuration

    Independent of the three-stage search: it retrieves Event/Chunk with the query vector directly
    and supports a hybrid search over the title/heading and content vectors.

    Example:
        config = VectorConfig(
            return_type="event",
            top_k=20,
            title_weight=0.3,
            content_weight=0.7,
            similarity_threshold=0.4
        )
    """

    # How many to return
    top_k: int = Field(
        default=20,
        ge=1, le=1000,
        description="maximum number finally returned"
    )

    # Return type
    return_type: Literal["chunk", "event"] = Field(
        default="event",
        description="return type: chunk=section (SourceChunk), event=event (SourceEvent)"
    )

    # Vector weights (title_weight + content_weight must equal 1.0)
    title_weight: float = Field(
        default=0.3,
        ge=0.0, le=1.0,
        description="title vector weight (title_vector for an event, heading_vector for a section)"
    )

    content_weight: float = Field(
        default=0.7,
        ge=0.0, le=1.0,
        description="content vector weight (content_vector)"
    )

    # Similarity threshold (used directly for recall)
    similarity_threshold: float = Field(
        default=0.4,
        ge=0.0, le=1.0,
        description="similarity threshold; results scoring below it are filtered out"
    )


class QueryNormalizationConfig(pipelineBaseModel):
    """
    Query normalisation configuration

    Preprocesses the query and extracts keywords:
    - text cleaning (lowercase, punctuation normalisation)
    - jieba tokenisation
    - stopword filtering (using the goto456/stopwords CJK stopword list)
    """

    # The single switch
    enabled: bool = Field(
        default=False,
        description="whether query normalisation is enabled (it adds tokenisation and stopword filtering)"
    )


class SearchBaseConfig(pipelineBaseModel):
    """
    Base search configuration

    Used by the engine layer; holds the base parameters plus the algorithm configuration
    """

    # Base parameters (the engine needs them)
    query: str = Field(..., description="search query")
    original_query: str = Field(default="", description="original query")
    start_time: Optional[datetime] = Field(
        default=None,
        description="start of the time range (optional, UTC; used by the ES time filter)",
    )
    end_time: Optional[datetime] = Field(
        default=None,
        description="end of the time range (optional; used by the time filter)",
    )
    source_ids: Optional[List[str]] = Field(
        default=None,
        description="event source ID list (Article/Conversation ID), optional, used for exact filtering",
    )

    @field_validator("start_time", "end_time", mode="after")
    @classmethod
    def _strip_timezone(cls, v):
        """
        The database stores UTC time (the MySQL connection sets time_zone='+00:00').
        The frontend sends ISO 8601 with a time zone offset (local time), which must become naive UTC to match the DB.
        """
        if v is not None and v.tzinfo is not None:
            return v.astimezone(timezone.utc).replace(tzinfo=None)
        return v

    # Feature switches
    enable_query_rewrite: bool = Field(
        default=True,
        description="enable query rewriting (turns colloquial phrasing into a more searchable question)"
    )

    # Query normalisation configuration
    query_normalization: QueryNormalizationConfig = Field(
        default_factory=QueryNormalizationConfig,
        description="query normalisation configuration"
    )

    # Entity type filter (used by both the Recall and Expand stages)
    exclude_entity_types: List[str] = Field(
        default=["start_time", "end_time"],
        description="[blocklist] entity types to exclude"
    )

    # Return type control
    return_type: ReturnType = Field(
        default=ReturnType.EVENT,
        description="return type: event or paragraph, events by default"
    )

    # Clue count control (governs both the Expand and Rerank stages)
    max_clues_per_event: int = Field(
        default=3,
        ge=1, le=5,
        description="Expand stage: caps the event-entity clues in both directions; Rerank stage: caps an event's entity clues"
    )

    # Section return control
    return_chunks: bool = Field(
        default=False,
        description="whether section information is returned (taken from the event's chunk_id, deduplicated in event order)"
    )

    # Rerank configuration
    rerank: RerankConfig = Field(
        default_factory=RerankConfig, description="rerank configuration")

    # Strategy-specific configuration (passed through to SearchConfig.strategy_config)
    strategy_config: Optional[Any] = Field(
        default=None,
        description="the strategy-specific configuration instance (MultiConfig/AtomicConfig/VectorConfig)"
    )


class SearchConfig(SearchBaseConfig):
    """
    Full search configuration (base configuration + runtime context)

    Extends SearchBaseConfig with the runtime context it needs

    Example:
        # Single-source search (backwards compatible)
        config = SearchConfig(
            query="artificial intelligence",
            source_config_id="source_123",
            recall=RecallConfig(max_entities=30),
            expand=ExpandConfig(max_hops=3),
            rerank=RerankConfig(strategy=RerankStrategy.MULTI)
        )

        # Multi-source search (new)
        config = SearchConfig(
            query="artificial intelligence",
            source_config_ids=["source_001", "source_002", "source_003"],
            recall=RecallConfig(max_entities=30),
            expand=ExpandConfig(max_hops=3),
            rerank=RerankConfig(strategy=RerankStrategy.MULTI)
        )
    """

    # === Runtime context ===
    source_config_id: Optional[str] = Field(None, description="data source ID (single, backwards compatible)")
    source_config_ids: Optional[List[str]] = Field(
        None, description="data source ID list (multi-source search)")
    article_id: Optional[str] = Field(None, description="article ID")
    background: Optional[str] = Field(None, description="background information")

    # === Strategy-specific configuration (optional; SAGSearcher passes it to the matching sub-searcher) ===
    strategy_config: Optional[Any] = Field(
        default=None,
        description="the strategy-specific configuration instance (MultiConfig/AtomicConfig/VectorConfig); "
                    "when given, SAGSearcher passes it to the matching sub-searcher, overriding that searcher's defaults"
    )

    def model_post_init(self, __context):
        """Post-initialisation validation and handling of source_config_id/source_config_ids"""
        # Validation: at least one must be given
        if not self.source_config_id and not self.source_config_ids:
            raise ValueError("Either source_config_id or source_config_ids must be given")

        # Normalisation: when only source_config_id is given, convert it into source_config_ids
        if self.source_config_id and not self.source_config_ids:
            self.source_config_ids = [self.source_config_id]
        elif self.source_config_ids and not self.source_config_id:
            # Multi-source case: source_config_id becomes the first one (backwards compatible)
            self.source_config_id = self.source_config_ids[0]

    def get_source_config_ids(self) -> List[str]:
        """
        Get the normalised source_config_ids list

        Returns:
            The source_config_ids list (at least one element)
        """
        return self.source_config_ids or []

    def is_multi_source(self) -> bool:
        """Whether this is a multi-source search"""
        return len(self.get_source_config_ids()) > 1


class AtomicConfig(pipelineBaseModel):
    """
    Atomic event retriever configuration

    Retrieves atomised triple events that hold exactly 2 entities.

    Example:
        config = AtomicConfig(
            top_k=20,
            similarity_threshold=0.4
        )
    """

    entity_top_k: int = Field(
        default=20,
        ge=1, le=1000,
        description="maximum number of entities returned"
    )
    atomic_top_k: int = Field(
        default=20,
        ge=1, le=1000,
        description="maximum number of atomised events"
    )

    key_similarity_threshold: float = Field(
        default=0.9,
        ge=0.0, le=1.0,
        description="similarity threshold; results scoring below it are filtered out"
    )

    similarity_threshold: float = Field(
        default=0.4,
        ge=0.0, le=1.0,
        description="similarity threshold of the event vector search"
    )

    max_hops: int = Field(
        default=1,
        ge=0, le=10,
        description="multi-hop expansion count (0=no expansion, 1=one round)"
    )

    max_events: int = Field(
        default=1000,
        ge=1, le=5000,
        description="maximum number of events returned by the coarse ranking"
    )

    rerank_top_k: int = Field(
        default=5,
        ge=1, le=20,
        description="how many the LLM selection returns"
    )

    max_sections: int = Field(
        default=10,
        ge=1, le=50,
        description="maximum number of sections finally returned (truncated after chunk_id deduplication)"
    )


class MultiConfig(pipelineBaseModel):
    """
    Unified configuration of the multi-element event retriever

    Three expansion strategies are supported:
    - "multi":   single-stage fixed-hop expansion (uses the max_hops parameter)
    - "multi1":  two-stage expansion; stage B seeds from every hop1 event entity (breadth first)
    - "hopllm":  two-stage expansion; stage B seeds from the coarse-ranked event entities (quality first)

    Example:
        # Single-stage strategy
        config = MultiConfig(strategy="multi", max_hops=2, max_events=100)

        # Two-stage strategy (multi1 or hopllm)
        config = MultiConfig(
            strategy="hopllm",
            max_events_a=100,
            max_events_b=50,
            max_hop_retries=3
        )
    """

    # ========== Strategy choice ==========
    strategy: str = Field(
        default="multi",
        description="expansion strategy: multi=single-stage fixed hops, multi1=two-stage every seed, hopllm=two-stage coarse-ranked seeds"
    )
    mode: Literal["fast", "precise"] = Field(
        default="fast",
        description="multi_vector mode: fast=BM25 entity recall + small expansion, precise=BM25 entity recall + LLM filtering"
    )
    spacy_model: str = Field(
        default="en_core_web_sm",
        description="[legacy] unused by multi_vector today; kept for compatibility with the old entity extraction flow"
    )

    # ========== Shared parameters ==========
    entity_top_k: int = Field(
        default=20,
        ge=1, le=1000,
        description="maximum number of entities the query->entity BM25 returns"
    )
    multi_top_k: int = Field(
        default=20,
        ge=1, le=100,
        description="how many the query->event vector recall returns; the precise entity->event channel is fixed at 40 in multi_vector"
    )
    key_similarity_threshold: float = Field(
        default=0.9,
        ge=0.0, le=1.0,
        description="[legacy] unused by multi_vector today; the similarity threshold of the old entity vector recall"
    )
    similarity_threshold: float = Field(
        default=0.4,
        ge=0.0, le=1.0,
        description="similarity threshold of the event vector search"
    )
    rerank_top_k: int = Field(
        default=10,
        ge=1, le=20,
        description="how many the LLM selection returns"
    )
    max_sections: int = Field(
        default=10,
        ge=1, le=50,
        description="maximum number of sections finally returned (truncated after chunk_id deduplication)"
    )
    # ========== Parameters specific to the multi strategy ==========
    max_hops: int = Field(
        default=1,
        ge=0, le=10,
        description="[multi] multi-hop expansion count (0=no expansion, 1=one round)"
    )
    max_events: int = Field(
        default=100,
        ge=1, le=500,
        description="[multi] maximum number of events returned by the coarse ranking"
    )
    max_expand_events_per_hop: int = Field(
        default=2000,
        ge=1, le=10000,
        description="[multi] maximum number of events the entity->event recall returns per hop during expansion"
    )

    # ========== Parameters specific to fast mode ==========
    fast_entity_k: int = Field(
        default=5,
        ge=1, le=100,
        description="[fast] how many entities are kept after key->entity"
    )
    fast_entity_event_candidate_k: int = Field(
        default=20,
        ge=1, le=100,
        description="[fast] initial entity->event candidate count, capped at 20 by default, then narrowed to fast_entity_event_k by query-content similarity"
    )
    fast_entity_event_k: int = Field(
        default=20,
        ge=1, le=100,
        description="[fast] how many event1 rows are kept inside the entity-filtered candidates by query-content similarity"
    )
    fast_query_event_k: int = Field(
        default=20,
        ge=1, le=100,
        description="[fast] how many the direct query->event2 vector recall returns"
    )
    fast_answer_k: int = Field(
        default=5,
        ge=1, le=100,
        description="[fast] how many first-hop events are kept by score after the event1/event2 union"
    )
    fast_expand_answer_k: int = Field(
        default=5,
        ge=0, le=100,
        description="[fast] how many event_set2 rows are kept by query-content similarity after the first-hop expansion"
    )
    fast_vector_weight: float = Field(
        default=0.85,
        ge=0.0,
        description="[fast] weight of the query-content vector score when ranking first-hop events before expand"
    )
    fast_entity_weight: float = Field(
        default=0.15,
        ge=0.0,
        description="[fast] weight of the entity-hit boost when ranking first-hop events before expand; hitting any fast entity scores 1, otherwise 0"
    )
    fast_channel_weight: float = Field(
        default=0.05,
        ge=0.0,
        description="[fast] weight of the dual-channel hit bonus when ranking first-hop events before expand"
    )

    # ========== Parameters specific to the multi1/hopllm strategies ==========
    max_events_a: int = Field(
        default=100,
        ge=1, le=5000,
        description="[multi1/hopllm] maximum candidate count for the Step6 coarse ranking of eventset (hop0+hop1)"
    )
    max_events_b: int = Field(
        default=0,
        ge=0, le=5000,
        description="[multi1/hopllm] target expansion count and maximum coarse-ranking candidates for eventset1 (hop2+)"
    )
    max_hop_retries: int = Field(
        default=3,
        ge=1, le=10,
        description="[multi1/hopllm] maximum retry hops in stage B"
    )


__all__ = [
    # Configuration
    "SearchConfig",
    "SearchBaseConfig",
    "RerankConfig",
    "VectorConfig",
    "AtomicConfig",
    "MultiConfig",
    "QueryNormalizationConfig",
    "RerankStrategy",
    "ReturnType",
]
