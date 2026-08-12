"""
Searcher - the unified entry point

Three search modes are offered:
1. VECTOR  - pure vector search (skips Recall/Expand and retrieves sections by vector directly)
2. ATOMIC  - atomic event retrieval (triples + LLM selection)
3. MULTI   - multi-element event retrieval (multi-entity + multi-hop expansion + LLM selection)
            the MultiConfig.strategy parameter selects one of three sub-strategies:
            - "multi": fixed-hop expansion
            - "multi1": two-stage expansion (every seed)
            - "hopllm": two-stage expansion (coarse-ranked seeds)
"""

import time
from typing import Dict, Any, Optional

from alicecore.core.prompt.manager import PromptManager
from alicecore.exceptions import SearchError
from alicecore.modules.search.config import SearchConfig, RerankStrategy, ReturnType, MultiConfig
from alicecore.modules.search.vector import VectorSearcher
from alicecore.modules.search.atomic import AtomicSearcher
from alicecore.utils import get_logger

logger = get_logger("search.searcher")


class SAGSearcher:
    """
    SAG searcher - offers three search modes

    1. VECTOR mode:
       - skips Recall/Expand and searches sections by vector directly
       - only the PARAGRAPH return type is supported

    2. ATOMIC mode:
       - atomic event retrieval (triples + LLM selection)
       - returns a section list

    3. MULTI mode:
       - multi-element event retrieval (multi-entity + multi-hop expansion + LLM selection)
       - the MultiConfig.strategy parameter selects one of three sub-strategies:
         * "multi": fixed-hop expansion
         * "multi1": two-stage expansion (every seed)
         * "hopllm": two-stage expansion (coarse-ranked seeds)
       - returns a section list

    Result shape:
    {
        "sections": List[Dict],        # the section list
        "clues": List[Dict],           # the clue list (feeds the frontend graph)
        "stats": Dict,                 # the statistics
        "query": Dict                  # the query information
    }
    """

    def __init__(
        self,
        prompt_manager: PromptManager,
        model_config: Optional[Dict] = None,
    ):
        """
        Initialise the searcher

        Args:
            prompt_manager: the prompt manager
            model_config: LLM configuration dictionary (optional)
        """
        self.prompt_manager = prompt_manager
        self.model_config = model_config
        self.logger = get_logger("search.sag")

        # Initialise the three searchers
        self._vector_searcher = VectorSearcher()
        self._atomic_searcher = AtomicSearcher()
        self._multi_searcher: Optional[Any] = None
        self._multi_es_searcher: Optional[Any] = None

        self.logger.info("SAG searcher initialised")

    def _get_multi_searcher(self) -> Any:
        if self._multi_searcher is None:
            from alicecore.modules.search.multi import MultiSearcher as MySQLMultiSearcher

            self._multi_searcher = MySQLMultiSearcher()
        return self._multi_searcher

    def _get_multi_es_searcher(self, config: MultiConfig) -> Any:
        if self._multi_es_searcher is None:
            from alicecore.modules.search.multi_vector import MultiSearcher as ESMultiSearcher

            self._multi_es_searcher = ESMultiSearcher(config=config)
        return self._multi_es_searcher

    def _get_multi_es_config(self, config: SearchConfig) -> MultiConfig:
        if isinstance(config.strategy_config, MultiConfig):
            return config.strategy_config
        if isinstance(config.strategy_config, dict):
            return MultiConfig(**config.strategy_config)
        return MultiConfig()

    async def search(self, config: SearchConfig) -> Dict[str, Any]:
        """
        Run the search

        Args:
            config: the search configuration

        Returns:
            {
                "sections": List[Dict],        # the section list
                "clues": List[Dict],           # the clue list
                "stats": Dict,                 # the statistics
                "query": Dict                  # the query information
            }
        """
        try:
            total_start = time.perf_counter()

            # Print the configuration parameters
            self.logger.info("=" * 100)
            self.logger.info("SAG search configuration:")
            self.logger.info("=" * 100)
            self.logger.info("Base parameters:")
            self.logger.info(f"  query: '{config.query}'")
            self.logger.info(f"  strategy: {config.rerank.strategy}")
            self.logger.info(f"  source_config_ids: {config.source_config_ids[:5] if config.source_config_ids else []}")
            # return_type only applies to the VECTOR strategy (and the effective value lives in strategy_config.return_type);
            # ATOMIC/MULTI/MULTI_ES always return sections and ignore the field, so it is not printed to avoid confusion
            if config.rerank.strategy == RerankStrategy.VECTOR:
                effective_return_type = getattr(
                    config.strategy_config, "return_type", config.return_type
                )
                self.logger.info(f"  return_type: {effective_return_type} (applies to VECTOR)")
            self.logger.info("=" * 100)

            self.logger.info(
                f"Search started: query='{config.query}', strategy={config.rerank.strategy}"
            )

            # Pick the search mode from the strategy
            strategy = config.rerank.strategy

            # VECTOR mode: pure vector search
            if strategy == RerankStrategy.VECTOR:
                if config.return_type != ReturnType.PARAGRAPH:
                    raise SearchError(
                        f"The VECTOR strategy only supports PARAGRAPH mode, got {config.return_type}"
                    )

                self.logger.info("=" * 60)
                self.logger.info("[VECTOR mode] skipping Recall/Expand, searching by vector directly")
                self.logger.info("=" * 60)

                vector_start = time.perf_counter()
                rerank_result = await self._vector_searcher.search_chunks_for_rerank(
                    query=config.query,
                    source_config_ids=config.get_source_config_ids(),
                    config=config.strategy_config if config.strategy_config is not None else config,
                )
                vector_time = time.perf_counter() - vector_start
                total_time = time.perf_counter() - total_start

                response = {
                    "sections": rerank_result.get("sections", []),
                    "clues": [],
                    "stats": {
                        "vector": {
                            "sections_count": len(rerank_result.get("sections", [])),
                        },
                        "timing": {
                            "vector": vector_time,
                            "total": total_time,
                        },
                    },
                    "query": {
                        "original": config.original_query or config.query,
                        "current": config.query,
                        "rewritten": False,
                    },
                }

                self.logger.info(
                    f"VECTOR search finished: {len(response['sections'])} sections in {total_time:.3f}s"
                )
                return response

            # ATOMIC mode: atomic event retrieval
            elif strategy == RerankStrategy.ATOMIC:
                self.logger.info("=" * 60)
                self.logger.info("[ATOMIC mode] atomic event retrieval")
                self.logger.info("=" * 60)

                atomic_start = time.perf_counter()
                rerank_result = await self._atomic_searcher.search_for_rerank(
                    query=config.query,
                    source_config_ids=config.get_source_config_ids(),
                    config=config.strategy_config if config.strategy_config is not None else config,
                )
                atomic_time = time.perf_counter() - atomic_start
                total_time = time.perf_counter() - total_start

                response = {
                    "sections": rerank_result.get("sections", []),
                    "clues": [],
                    "stats": {
                        "atomic": {
                            "sections_count": len(rerank_result.get("sections", [])),
                        },
                        "timing": {
                            "atomic": atomic_time,
                            "total": total_time,
                        },
                    },
                    "query": {
                        "original": config.original_query or config.query,
                        "current": config.query,
                        "rewritten": False,
                    },
                }

                self.logger.info(
                    f"ATOMIC search finished: {len(response['sections'])} sections in {total_time:.3f}s"
                )
                return response

            # MULTI mode: multi-element event retrieval
            elif strategy == RerankStrategy.MULTI:
                self.logger.info("=" * 60)
                self.logger.info("[MULTI mode] multi-element event retrieval")
                self.logger.info("=" * 60)

                multi_start = time.perf_counter()
                multi_searcher = self._get_multi_searcher()
                rerank_result = await multi_searcher.search_for_rerank(
                    query=config.query,
                    source_config_ids=config.get_source_config_ids(),
                    config=config.strategy_config if config.strategy_config is not None else config,
                )
                multi_time = time.perf_counter() - multi_start
                total_time = time.perf_counter() - total_start

                response = {
                    "sections": rerank_result.get("sections", []),
                    "clues": [],
                    "stats": {
                        "multi": {
                            "sections_count": len(rerank_result.get("sections", [])),
                        },
                        "timing": {
                            "multi": multi_time,
                            "total": total_time,
                        },
                    },
                    "query": {
                        "original": config.original_query or config.query,
                        "current": config.query,
                        "rewritten": False,
                    },
                }

                self.logger.info(
                    f"MULTI search finished: {len(response['sections'])} sections in {total_time:.3f}s"
                )
                return response

            # MULTI_ES mode: ES-first multi-element event retrieval
            elif strategy == RerankStrategy.MULTI_ES:
                multi_config = self._get_multi_es_config(config)

                self.logger.info("=" * 60)
                self.logger.info(
                    f"[MULTI_ES mode] ES multi-element event retrieval, mode={multi_config.mode}"
                )
                self.logger.info("=" * 60)

                multi_start = time.perf_counter()
                multi_es_searcher = self._get_multi_es_searcher(multi_config)
                await multi_es_searcher.warmup(multi_config)
                rerank_result = await multi_es_searcher.search_for_rerank(
                    query=config.query,
                    source_config_ids=config.get_source_config_ids(),
                    config=multi_config,
                )
                multi_time = time.perf_counter() - multi_start
                total_time = time.perf_counter() - total_start

                response = {
                    "sections": rerank_result.get("sections", []),
                    "clues": [],
                    "stats": {
                        "multi_es": {
                            "sections_count": len(rerank_result.get("sections", [])),
                            "mode": multi_config.mode,
                        },
                        "timing": {
                            "multi_es": multi_time,
                            "total": total_time,
                        },
                    },
                    "query": {
                        "original": config.original_query or config.query,
                        "current": config.query,
                        "rewritten": False,
                    },
                }

                self.logger.info(
                    f"MULTI_ES search finished: {len(response['sections'])} sections in {total_time:.3f}s"
                )
                return response

            else:
                raise SearchError(f"Unsupported search strategy: {strategy}")

        except Exception as e:
            self.logger.error(f"Search failed: {e}", exc_info=True)
            raise SearchError(f"Search failed: {e}") from e


# Backwards compatible alias
EventSearcher = SAGSearcher

__all__ = [
    "SAGSearcher",
    "EventSearcher",
]
