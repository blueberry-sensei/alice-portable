"""
Load module configuration classes and result models

Defines the configuration options and the return shape of document loading
"""

from pathlib import Path
from typing import Dict, List, Optional, Union

from pydantic import Field, field_validator, model_validator

from alicecore.models.base import pipelineBaseModel


# ============ Result models ============

class LoadResult(pipelineBaseModel):
    """
    Load result (one return shape)

    Bridges the Load -> Extract flow
    """

    # === Core data ===
    source_id: str = Field(..., description="source ID (article_id)")
    source_type: str = Field(..., description="source type (ARTICLE)")
    chunk_ids: List[str] = Field(..., description="the generated chunk ID list")

    # === Metadata ===
    source_config_id: str = Field(..., description="source configuration ID")
    title: Optional[str] = Field(default=None, description="title")
    chunk_count: int = Field(..., description="chunk count")

    # === Extra data ===
    extra: Dict = Field(default_factory=dict, description="extra information")


# ============ Configuration models ============


class LoadBaseConfig(pipelineBaseModel):
    """
    Base load configuration

    Holds the configuration parameters common to every data source
    """

    # === Common configuration ===
    max_tokens: int = Field(
        default=1000,
        ge=100,
        le=100000,
        description="maximum tokens per chunk"
    )

    # === Storage configuration ===
    auto_vector: bool = Field(
        default=True,
        description="whether to index into Elasticsearch automatically"
    )

    # === Prompt enrichment ===
    background: Optional[str] = Field(
        default=None,
        description="background information (extra context for metadata generation)"
    )

    # === Source ===
    source_config_id: Optional[str] = Field(default=None, description="source ID")

    @field_validator('source_config_id')
    @classmethod
    def validate_source_config_id(cls, v):
        """Validate that source_config_id is not an empty string"""
        if v is not None and (not v or not v.strip()):
            raise ValueError("source_config_id cannot be an empty string")
        return v.strip() if v else v

    # === Batch processing configuration ===
    enable_batch_indexing: bool = Field(
        default=True,
        description="whether batch indexing optimisation is enabled"
    )

    embedding_batch_size: int = Field(
        default=10,
        ge=1,
        le=100,
        description="vector generation batch size (texts per batch)"
    )

    es_bulk_index_size: int = Field(
        default=50,
        ge=1,
        le=200,
        description="ES batch index size (documents per batch)"
    )


class DocumentLoadConfig(LoadBaseConfig):
    """Document load configuration - loads a Markdown document from a file path"""

    # === Data source ===
    path: Optional[Union[str, Path]] = Field(
        default=None,
        description="file path"
    )

    # === Document processing configuration ===
    min_content_length: int = Field(
        default=100,
        ge=10,
        description="minimum content length (in characters)"
    )
    merge_short_sections: bool = Field(
        default=False,
        description="whether short sections are merged"
    )
    chunk_mode: str = Field(
        default="standard",
        description=(
            "chunking mode: standard (smart splitting, short sections may merge), "
            "heading_strict (split strictly on headings, short sections never merge), "
            "overlap (fixed 1200-token chunks with a 100-token overlap)"
        )
    )

    @model_validator(mode='after')
    def check_path(self) -> 'DocumentLoadConfig':
        """Validate the file path"""
        if not self.path:
            raise ValueError("path must be given")
        return self

    @model_validator(mode='after')
    def normalize_chunk_mode(self) -> 'DocumentLoadConfig':
        allowed_modes = {"standard", "heading_strict", "overlap"}
        if self.chunk_mode not in allowed_modes:
            raise ValueError(f"Unsupported chunk_mode: {self.chunk_mode}")
        return self
