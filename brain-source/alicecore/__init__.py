"""alicecore - the SAG data engine (ingest / extract / search / query).

Main entry point::

    from alicecore import DataEngine, EngineConfig

Import the details from the submodules as needed (following the usual library convention, keeping the top level small):

- configuration submodels -> ``alicecore.config``     (RelationalConfig / MySQLConfig / ESConfig / LLMConfig / LLMProviderConfig / EmbeddingConfig / RerankConfig / EntityTypeConfig)
- result types            -> ``alicecore.results``    (IngestResult / ExtractResult / SearchResult / ChunkResult / ChunkItem)
- exception hierarchy     -> ``alicecore.exceptions`` (all inheriting :class:`SagError`)
"""

from alicecore.__about__ import __version__
from alicecore.config import EngineConfig
from alicecore.engine import DataEngine
from alicecore.exceptions import SagError

__all__ = ["__version__", "DataEngine", "EngineConfig", "SagError"]
