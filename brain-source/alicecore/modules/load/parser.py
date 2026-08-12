"""
Document parser

Built on the smart chunker, giving token-level precision
Only Markdown documents are supported
"""

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from alicecore.exceptions import LoadError
from alicecore.modules.load.chunking import (
    ChunkingResult,
    MarkdownArticleSectionBuilder,
    MarkdownBlockParser,
    MarkdownInputNormalizer,
    MarkdownSourceChunkAssembler,
    RAGChunkingPipeline,
)
from alicecore.modules.load.chunking.types import (
    BlockType,
    ChunkDraft,
    InputDocument,
    SectionDraft,
    StructuredBlock,
)
from alicecore.utils import (
    TokenEstimator,
    get_logger,
    normalize_heading_text,
)

logger = get_logger("modules.load.parser")


class MarkdownParser:
    """Markdown document parser (supports the standard / heading_strict chunking modes)"""

    def __init__(
        self,
        max_tokens: int = 1000,
        model_type: str = "generic",
        section_max_tokens: Optional[int] = None,
        chunk_mode: str = "standard",
    ) -> None:
        """
        Initialise the parser

        Args:
            max_tokens: maximum tokens per chunk
            model_type: the model type used for token estimation
            section_max_tokens: soft cap of ArticleSection tokens (derived automatically by default)
            chunk_mode: chunking mode (standard / heading_strict / overlap)
                - standard: greedy aggregation, merging small blocks across headings
                - heading_strict: a new heading forces a break, and each heading becomes its own chunk
                - overlap: same as standard (overlap is not implemented yet, it behaves like standard)
        """
        self.max_tokens = max_tokens
        self.chunk_mode = chunk_mode
        self.token_estimator = TokenEstimator(model_type)
        self.section_max_tokens = section_max_tokens or max(128, min(512, max_tokens // 4))
        self._last_chunking_result: Optional[ChunkingResult] = None
        heading_strict = (chunk_mode == "heading_strict")
        self.chunking_pipeline = RAGChunkingPipeline(
            input_normalizer=MarkdownInputNormalizer(),
            block_parser=MarkdownBlockParser(),
            section_builder=MarkdownArticleSectionBuilder(
                section_max_tokens=self.section_max_tokens,
                model_type=model_type,
            ),
            chunk_assembler=MarkdownSourceChunkAssembler(
                source_chunk_max_tokens=max_tokens,
                model_type=model_type,
                heading_strict=heading_strict,
            ),
        )

        # Heading regular expression
        self.heading_pattern = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)

        logger.info(
            "Document parser initialised",
            extra={
                "max_tokens": max_tokens,
                "model_type": model_type,
                "section_max_tokens": self.section_max_tokens,
                "chunk_mode": chunk_mode,
            }
        )

    def parse_file(self, file_path: Path) -> tuple[str, int]:
        """
        Parse a Markdown file

        Args:
            file_path: the Markdown file path (.md / .markdown)

        Returns:
            (the full content, the chunk count)

        Raises:
            LoadError: reading the file failed
        """
        content, result = asyncio.run(self.parse_file_with_plan_async(file_path))
        return content, len(result.source_chunks)

    def parse_file_with_plan(self, file_path: Path) -> tuple[str, ChunkingResult]:
        """Synchronous wrapper: parse the file and return the two-layer chunking result"""
        return asyncio.run(self.parse_file_with_plan_async(file_path))

    async def parse_file_async(self, file_path: Path) -> tuple[str, int]:
        """Parse a Markdown file asynchronously, returning (content, chunk_count)"""
        content, result = await self.parse_file_with_plan_async(file_path)
        return content, len(result.source_chunks)

    async def parse_file_with_plan_async(self, file_path: Path) -> tuple[str, ChunkingResult]:
        """
        Parse the file and return the two-layer chunking result (SectionDraft + SourceChunk)
        """
        try:
            logger.info(f"Parsing the file: {file_path.name} ({file_path.suffix})")

            if not file_path.exists():
                raise LoadError(f"The file does not exist: {file_path}")

            file_suffix = file_path.suffix.lower()
            if file_suffix not in {'.md', '.markdown'}:
                raise LoadError(
                    f"Unsupported file format: {file_path.suffix}, only .md / .markdown are supported"
                )

            content = file_path.read_text(encoding="utf-8")

            result = await self.parse_content_with_plan_async(
                content,
                source_path=file_path.parent,
            )

            logger.info(
                f"File parsed: {file_path.name}",
                extra={
                    "article_sections": len(result.article_sections),
                    "source_chunks": len(result.source_chunks),
                },
            )
            return content, result

        except Exception as e:
            logger.error(f"Parsing the file failed: {file_path}: {e}", exc_info=True)
            raise LoadError(f"Parsing the file failed: {e}") from e

    def parse_content_with_plan(
        self,
        content: str,
        source_path: Optional[Path] = None,
    ) -> ChunkingResult:
        """
        Synchronous wrapper: returns the two-layer chunking result (SectionDraft + SourceChunk)
        """
        return asyncio.run(
            self.parse_content_with_plan_async(
                content=content,
                source_path=source_path,
            )
        )

    async def parse_content_with_plan_async(
        self,
        content: str,
        source_path: Optional[Path] = None,
    ) -> ChunkingResult:
        """Return the two-layer chunking result (SectionDraft + SourceChunk) asynchronously"""
        if self.chunk_mode == "heading_strict":
            result = self._parse_content_heading_strict(content, source_path)
        else:
            result = await self.chunking_pipeline.run_async(content, source_path=source_path)
        self._last_chunking_result = result
        return result

    def _parse_content_heading_strict(
        self,
        content: str,
        source_path: Optional[Path] = None,
    ) -> ChunkingResult:
        """
        Legacy heading_strict chunking: split on heading boundaries, keeping each heading block whole as one chunk,
        without a sentence-level split, and preserving the original space-joined text layout.
        """
        sections = self._extract_sections_heading_strict(content)
        source_chunks: List[ChunkDraft] = []
        section_drafts: List[SectionDraft] = []

        for idx, section in enumerate(sections):
            headings = section["headings"]
            content_lines = section["content_lines"]

            if headings:
                min_level = min(h[0] for h in headings)
                main_heading = next(h[1] for h in headings if h[0] == min_level)
                heading_content = "\n".join(h[2] for h in headings)
                content_text = "\n".join(content_lines).strip()
                if content_text:
                    full_content = heading_content + "\n" + content_text
                else:
                    full_content = heading_content
                normalized_heading = normalize_heading_text(main_heading)
            else:
                normalized_heading = ""
                full_content = "\n".join(content_lines).strip()

            if not full_content:
                continue

            section_drafts.append(
                SectionDraft(
                    order_index=idx,
                    render_group_index=idx,
                    heading=normalized_heading,
                    content=full_content,
                    raw_content=full_content,
                    section_type="TEXT",
                    metadata={"legacy_mode": "heading_strict"},
                )
            )
            source_chunks.append(
                ChunkDraft(
                    rank=idx,
                    heading=normalized_heading,
                    content=full_content,
                    raw_content=full_content,
                    chunk_type="TEXT",
                    section_order_indices=[idx],
                    metadata={"legacy_mode": "heading_strict"},
                )
            )

        doc = InputDocument(
            content=content,
            source_path=source_path,
            is_markdown=True,
            metadata={"legacy_mode": "heading_strict"},
        )
        blocks = [
            StructuredBlock(
                block_id="legacy-0",
                block_type=BlockType.TEXT,
                raw_content=content,
                heading="",
                start_index=0,
                end_index=len(content),
                metadata={"legacy_mode": "heading_strict"},
            )
        ]
        return ChunkingResult(
            input_doc=doc,
            blocks=blocks,
            article_sections=section_drafts,
            source_chunks=source_chunks,
        )

    def _extract_sections_heading_strict(self, content: str) -> List[Dict]:
        """
        Split the sections on headings in heading-strict mode (the legacy implementation).

        Every new heading starts a new section, each heading plus the content after it is kept whole,
        there is no sentence-level split, and the original space-joined text layout is preserved.

        Returns:
            The section list, each shaped:
            {"headings": [(level, title, heading_line), ...], "content_lines": [...]}
        """
        lines = content.split("\n")
        sections = []
        current_section: Dict = {"headings": [], "content_lines": []}

        for line in lines:
            heading_match = self.heading_pattern.match(line)
            if heading_match:
                if current_section["headings"] or current_section["content_lines"]:
                    sections.append(current_section)
                level = len(heading_match.group(1))
                title = heading_match.group(2).strip()
                current_section = {
                    "headings": [(level, title, line)],
                    "content_lines": [],
                }
            else:
                current_section["content_lines"].append(line)

        if current_section["headings"] or current_section["content_lines"]:
            sections.append(current_section)

        logger.debug(f"[heading-strict mode] extracted {len(sections)} sections")
        return sections

    def get_last_chunking_result(self) -> Optional[ChunkingResult]:
        """Return the two-layer chunking result of the most recent parse"""
        return self._last_chunking_result

    def extract_title(self, content: str) -> str:
        """
        Extract the title from the Markdown content (the first level-one heading)

        Args:
            content: the Markdown text

        Returns:
            The title, or "Untitled" when there is none

        Example:
            >>> parser = MarkdownParser()
            >>> title = parser.extract_title("# My Title\\n\\nContent")
            >>> print(title)  # "My Title"
        """
        match = self.heading_pattern.search(content)
        if match:
            return normalize_heading_text(match.group(2))
        return "Untitled"

    def _normalize_heading(self, heading: Optional[str]) -> str:
        """Normalise the title, so an over-long raw Markdown heading line cannot fail model validation."""
        return normalize_heading_text(heading)
