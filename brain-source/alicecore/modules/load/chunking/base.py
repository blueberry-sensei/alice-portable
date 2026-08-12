"""
The four base classes of the RAG chunking layers
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

from alicecore.modules.load.chunking.types import (
    ChunkDraft,
    InputDocument,
    SectionDraft,
    StructuredBlock,
)


class BaseInputNormalizer(ABC):
    """Input layer: one input shape"""

    @abstractmethod
    def normalize(self, content: str, source_path: Optional[Path] = None) -> InputDocument:
        pass


class BaseBlockParser(ABC):
    """Structure recognition layer: recognises the block structure"""

    @abstractmethod
    def parse_blocks(self, doc: InputDocument) -> List[StructuredBlock]:
        pass


class BaseArticleSectionBuilder(ABC):
    """ArticleSection generation layer: produces the smallest citable unit"""

    @abstractmethod
    async def build_sections(
        self,
        doc: InputDocument,
        blocks: List[StructuredBlock],
    ) -> List[SectionDraft]:
        pass


class BaseSourceChunkAssembler(ABC):
    """SourceChunk assembly layer: aimed at embedding and retrieval"""

    @abstractmethod
    def assemble_chunks(
        self,
        doc: InputDocument,
        sections: List[SectionDraft],
    ) -> List[ChunkDraft]:
        pass
