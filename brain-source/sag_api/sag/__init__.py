"""The alicecore adapter layer - the only place in the project that imports `alicecore`.

It exposes only sag's own DTOs and `EngineManager`, decoupling the engine implementation from the domain logic,
so replacing or upgrading the engine later stays inside this directory.
"""

from sag_api.sag.dto import (
    ChunkInfo,
    EntityInfo,
    GraphAssociationInfo,
    GraphEventInfo,
    ProcessOutcome,
    RetrievedSection,
    SearchOutcome,
    SourceGraphInfo,
)
from sag_api.sag.engine_manager import EngineManager

__all__ = [
    "ChunkInfo",
    "EngineManager",
    "EntityInfo",
    "GraphAssociationInfo",
    "GraphEventInfo",
    "ProcessOutcome",
    "RetrievedSection",
    "SearchOutcome",
    "SourceGraphInfo",
]
