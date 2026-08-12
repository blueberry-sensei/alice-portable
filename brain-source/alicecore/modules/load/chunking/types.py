"""
Core type definitions of the RAG chunking framework
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional


class BlockType(str, Enum):
    """Structural block type"""

    TEXT = "TEXT"
    FORMULA = "FORMULA"
    TABLE = "TABLE"
    CODE = "CODE"
    IMAGE = "IMAGE"


@dataclass
class InputDocument:
    """A document normalised by the input layer"""

    content: str
    source_path: Optional[Path] = None
    is_markdown: bool = True
    metadata: Dict = field(default_factory=dict)


@dataclass
class StructuredBlock:
    """Output of the structure recognition layer"""

    block_id: str
    block_type: BlockType
    raw_content: str
    heading: str = ""
    start_index: int = 0
    end_index: int = 0
    metadata: Dict = field(default_factory=dict)


@dataclass
class SectionDraft:
    """ArticleSection draft"""

    order_index: int
    render_group_index: int
    heading: str
    content: str
    raw_content: str
    section_type: str
    metadata: Dict = field(default_factory=dict)


@dataclass
class ChunkDraft:
    """SourceChunk draft"""

    rank: int
    heading: str
    content: str
    raw_content: str
    chunk_type: str
    section_order_indices: List[int]
    metadata: Dict = field(default_factory=dict)


@dataclass
class ChunkingResult:
    """Result of the whole chunking chain"""

    input_doc: InputDocument
    blocks: List[StructuredBlock]
    article_sections: List[SectionDraft]
    source_chunks: List[ChunkDraft]
