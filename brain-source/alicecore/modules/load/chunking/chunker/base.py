"""Base class for the Block -> Section split."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from alicecore.modules.load.chunking.types import SectionDraft, StructuredBlock


class BaseBlockChunker(ABC):
    """The smallest unit producing a SectionDraft from a block type."""

    @abstractmethod
    async def build_sections(
        self,
        block: StructuredBlock,
        order_start: int,
        render_group_index: int,
    ) -> List[SectionDraft]:
        pass
