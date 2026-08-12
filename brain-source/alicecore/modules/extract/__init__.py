"""
Extract module - event extraction

Flow: chunks -> processor(LLM) -> parser -> saver
"""

from alicecore.modules.extract.config import ExtractBaseConfig, ExtractConfig
from alicecore.modules.extract.parser import ResultParser
from alicecore.modules.extract.processor import EventProcessor
from alicecore.modules.extract.saver import EventSaver
from alicecore.modules.extract.extractor import EventExtractor

__all__ = [
    "EventExtractor",
    "EventProcessor",
    "ResultParser",
    "EventSaver",
    "ExtractBaseConfig",
    "ExtractConfig",
]
