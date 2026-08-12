"""
RAG chunking orchestrator
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from alicecore.modules.load.chunking.base import (
    BaseArticleSectionBuilder,
    BaseBlockParser,
    BaseInputNormalizer,
    BaseSourceChunkAssembler,
)
from alicecore.modules.load.chunking.types import ChunkingResult


class RAGChunkingPipeline:
    """The chunking pipeline that runs the 4 layers in order"""

    def __init__(
        self,
        input_normalizer: BaseInputNormalizer,
        block_parser: BaseBlockParser,
        section_builder: BaseArticleSectionBuilder,
        chunk_assembler: BaseSourceChunkAssembler,
    ) -> None:
        self.input_normalizer = input_normalizer
        self.block_parser = block_parser
        self.section_builder = section_builder
        self.chunk_assembler = chunk_assembler

    async def run_async(self, content: str, source_path: Optional[Path] = None) -> ChunkingResult:
        doc = self.input_normalizer.normalize(content, source_path=source_path)
        blocks = self.block_parser.parse_blocks(doc)
        sections = await self.section_builder.build_sections(doc, blocks)
        chunks = self.chunk_assembler.assemble_chunks(doc, sections)
        return ChunkingResult(
            input_doc=doc,
            blocks=blocks,
            article_sections=sections,
            source_chunks=chunks,
        )

    def run(self, content: str, source_path: Optional[Path] = None) -> ChunkingResult:
        return asyncio.run(self.run_async(content, source_path=source_path))
