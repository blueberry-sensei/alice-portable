"""
Vector retriever

A vector retriever independent of the three stages: it retrieves Event/Chunk with the query vector directly
and supports a hybrid search over the title/heading and content vectors.

Usage example:
    from alicecore.modules.search import VectorSearcher, VectorConfig

    config = VectorConfig(
        return_type="event",
        top_k=20,
        title_weight=0.3,
        content_weight=0.7,
        similarity_threshold=0.4
    )

    searcher = VectorSearcher()
    events = await searcher.search(
        query="advances in artificial intelligence",
        source_config_ids=["source_1", "source_2"],
        config=config
    )
"""

import time
from typing import Any, Dict, List, Optional, Union

from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload

from alicecore.core.storage.client import get_es_client
from alicecore.db import SourceEvent, SourceChunk, get_session_factory
from alicecore.modules.load.processor import DocumentProcessor
from alicecore.modules.search.config import VectorConfig
from alicecore.utils import get_logger

logger = get_logger("search.vector")


class VectorSearcher:
    """
    Vector retriever

    Supports a hybrid vector search over sections (SourceChunk) and events (SourceEvent).
    An ES script_score query computes the hybrid title/heading + content similarity.
    """

    INDEX_EVENTS = "event_vectors"
    INDEX_CHUNKS = "source_chunks"

    def __init__(self):
        """Initialise the vector retriever"""
        self.es_client = get_es_client()
        self.session_factory = get_session_factory()
        self.processor = DocumentProcessor()

    async def search_chunks_for_rerank(
        self,
        query: str,
        source_config_ids: List[str],
        query_vector: Optional[List[float]] = None,
        config: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Run the vector search and return sections

        Keeps the interface name the old SAGSearcher needs; internally it only does a vector recall.
        It uses an ES kNN vector search (avoiding the compilation limits of script_score).

        Args:
            query: the query text
            source_config_ids: source ID list
            query_vector: an optional precomputed vector (avoids recomputing)
            config: the SearchConfig object

        Returns:
            {
                "sections": [...],  # the section list, in descending similarity
                "_timings": {...}   # the timing statistics
            }
        """
        start_time = time.perf_counter()

        # Read the parameters from VectorConfig / SearchConfig
        top_k = 20
        min_score = 0.0

        if config:
            top_k = getattr(config, "top_k", top_k)
            min_score = getattr(config, "similarity_threshold", min_score)

        logger.info("=" * 60)
        logger.info(f"[vector search] query: '{query}'")
        logger.info(
            f"  top_k={top_k}, min_score={min_score}"
        )
        logger.info("=" * 60)

        # Step 1: generate the query vector
        vector_time = 0.0
        if query_vector is None:
            vector_start = time.perf_counter()
            query_vector = await self.processor.generate_embedding(query)
            vector_time = time.perf_counter() - vector_start
            logger.info(f"Vector generated, dimensions={len(query_vector)}, took {vector_time:.3f}s")
        else:
            logger.info(f"Using the precomputed vector, dimensions={len(query_vector)}")

        # Step 2: use the kNN search of SourceChunkRepository (avoiding the script_score compilation limits)
        from alicecore.core.storage.repositories.source_chunk_repository import SourceChunkRepository
        from alicecore.core.storage.client import get_es_client

        es_client = get_es_client()
        chunk_repo = SourceChunkRepository(es_client)

        es_start = time.perf_counter()
        es_results = await chunk_repo.search_similar_by_content(
            query_vector=query_vector,
            k=top_k,
            source_config_ids=source_config_ids,
        )
        es_time = time.perf_counter() - es_start
        logger.info(f"ES kNN search finished, {len(es_results)} sections hit, took {es_time:.3f}s")

        if not es_results:
            logger.info("[vector search] no matching section was found")
            total_time = time.perf_counter() - start_time
            return {
                "sections": [],
                "_timings": {
                    "vector_gen": vector_time,
                    "es_search": es_time,
                    "total": total_time,
                }
            }

        # Step 3: format the results (an ES kNN returns the full document)
        sections = []
        for result in es_results:
            score = result.get("_score", 0.0)
            if score < min_score:
                continue

            sections.append({
                "chunk_id": result.get("chunk_id"),
                "source_id": result.get("source_id"),
                "source_config_id": result.get("source_config_id"),
                "heading": result.get("heading"),
                "content": result.get("content"),
                "rank": result.get("rank"),
                "score": score,
                "weight": score,
            })

        sections = sorted(sections, key=lambda x: x["score"], reverse=True)

        sections = sections[:top_k]

        total_time = time.perf_counter() - start_time

        logger.info("=" * 60)
        logger.info(f"[vector search] finished, {len(sections)} sections in {total_time:.3f}s")
        logger.info("=" * 60)

        # Top-5 log
        for i, sec in enumerate(sections[:5]):
            heading = sec.get("heading", "")[:40] if sec.get("heading") else "untitled"
            logger.info(f"  Top-{i+1}: score={sec['score']:.4f} | {heading}...")

        return {
            "sections": sections,
            "_timings": {
                "vector_gen": vector_time,
                "es_search": es_time,
                "total": total_time,
            }
        }


__all__ = ["VectorSearcher"]
