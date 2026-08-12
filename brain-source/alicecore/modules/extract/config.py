"""
Extract module configuration classes

Defines the configuration options of event extraction
"""

from typing import List, Optional

from pydantic import Field, model_validator

from alicecore.models.base import pipelineBaseModel
from alicecore.models.entity import CustomEntityType


class ExtractBaseConfig(pipelineBaseModel):
    """
    Base extraction configuration

    Holds the base parameters of extraction behaviour and can be preset on the Engine
    """

    # ==================== Concurrency ====================
    max_concurrency: int = Field(
        default=5, ge=1, le=100, description="maximum concurrency (how many chunks agents process at once)"
    )

    # ==================== Vector sync ====================
    enable_event_vector_sync: bool = Field(default=True, description="whether event vectors are synced to the vector store")
    enable_entity_vector_sync: bool = Field(
        default=False, description="whether entity vectors are synced to the vector store (experimental)"
    )
    enable_event_entity_vector_sync: bool = Field(
        default=False, description="whether event-entity relation description vectors are synced to the vector store (experimental)"
    )
    embedding_batch_size: int = Field(
        default=10,
        ge=1,
        le=100,
        description="vector generation batch size (texts per embedding API call, bounded by the API)",
    )
    index_batch_size: int = Field(
        default=50,
        ge=1,
        le=500,
        description="index batch size (documents written to the vector store per batch)",
    )
    embedding_max_length: int = Field(
        default=500,
        ge=100,
        le=1000,
        description="maximum embedding input length in characters (bounded by the model's token limit; CJK is roughly 1.5 tokens per character, 500-800 recommended)",
    )

    # ==================== Context ====================
    prev_chunk_count: int = Field(
        default=1,
        ge=0,
        le=5,
        description="how many preceding chunks to load (background context for extraction; 0 loads none)",
    )
    max_content_length: int = Field(
        default=3000, ge=500, description="maximum content length (a safety truncation, so an unusually long text cannot hurt performance)"
    )

    # ==================== Quality filtering ====================
    chunk_min_length: int = Field(
        default=20, ge=0, description="minimum chunk content length; anything shorter is skipped (0 disables the filter)"
    )
    event_min_length: int = Field(default=15, ge=0, description="minimum event body length (filters clickbait)")
    text_min_length: int = Field(default=10, ge=0, description="minimum plain text length (filters link-only content)")
    filter_image_sections: bool = Field(
        default=False,
        description="whether image sections are filtered out (they take no part in extraction and only add noise; a chunk with no body left is skipped)",
    )
    filter_keywords: List[str] = Field(
        default_factory=lambda: ["scan the qr code", "join the group", "coupon", "claim now", "free for a limited time"],
        description="keyword blocklist (a match counts as a low-quality signal)",
    )

    @model_validator(mode='after')
    def set_local_defaults(self):
        """LOCAL mode turns vector sync on automatically (there is no MySQL embedding table)"""
        from alicecore.core.config import get_settings

        settings = get_settings()
        if settings.server_type == "LOCAL":
            # LOCAL mode must enable entity and event_entity vector sync
            if not self.enable_entity_vector_sync:
                self.enable_entity_vector_sync = True
            if not self.enable_event_entity_vector_sync:
                self.enable_event_entity_vector_sync = True
        return self

    # ==================== Entities ====================
    custom_entity_types: List[CustomEntityType] = Field(
        default_factory=list, description="custom entity type list (highest priority at runtime)"
    )

    # ==================== Historical recall ====================
    enable_related_events: bool = Field(
        default=True, description="whether historical events are recalled as background for the LLM extraction"
    )
    related_events_top_k: int = Field(default=3, ge=1, le=10, description="how many historical events to recall")
    related_events_threshold: float = Field(
        default=0.7, ge=0.0, le=1.0, description="historical event similarity threshold (anything lower is not recalled)"
    )

    # ==================== Prompt injection ====================
    timezone: str = Field(default="Asia/Shanghai", description="time zone (used when the prompt shows a time)")
    custom_background: str = Field(
        default="", description="custom background information (appended to the prompt's Background section)"
    )
    custom_requirements: str = Field(
        default="", description="custom extraction requirements (appended to the prompt's Requirements section)"
    )
    enable_strict_filtering: bool = Field(
        default=True, description="whether strict content filtering is enabled (the strict rules are passed into custom_requirements)"
    )
    test_mode: bool = Field(
        default=False, description="test mode: read test_extract.yaml instead of extract.yaml, and the entity_types_test list instead of the entity_types table"
    )


class ExtractConfig(ExtractBaseConfig):
    """
    Event extraction configuration - the full configuration (base + runtime context)

    The runtime context can be supplied in three ways:
    1. pass chunk_ids directly (a standalone call)
    2. the Engine sets chunk_ids after processing (a chained call)
    3. the Engine reads chunk_ids from the context automatically (automatic mode)
    """

    # ==================== Runtime context ====================
    source_config_id: str = Field(..., description="source ID")
    article_id: Optional[str] = Field(
        default=None, description="document ID (used for document-level entity type configuration)"
    )
    chunk_ids: List[str] = Field(..., min_length=1, description="chunk ID list")
    # Note: enable_strict_filtering and test_mode are inherited from ExtractBaseConfig and need no redefinition
