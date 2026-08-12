"""
Load module - document loading and processing

Loads a document, parses its structure, generates the metadata and computes the vectors
"""

from alicecore.modules.load.config import DocumentLoadConfig
from alicecore.modules.load.loader import (
    BaseLoader,
    DocumentLoader,
)
from alicecore.modules.load.parser import MarkdownParser
from alicecore.modules.load.processor import DocumentProcessor

__all__ = [
    "DocumentLoadConfig",
    "BaseLoader",
    "DocumentLoader",
    "MarkdownParser",
    "DocumentProcessor",
]
