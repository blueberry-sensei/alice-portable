"""
Multi-element event retriever (the ES-first version)

Differences from multi.py:
- Step3 channel 1: ES event_entity_vectors replaces the MySQL EventEntity JOIN SourceEvent
- Step4:           ES event_vectors replaces the two-table MySQL SourceEvent + EventEntity query
- Step5:           ES event_entity_vectors + event_vectors replace the multi-table MySQL JOIN
- Step7:           the LLM filter prompt drops thought_process and returns only the ID list (fewer output tokens)
- Step8:           still MySQL (the event_vectors index has no chunk_id field)

Today Step1 recalls entities with a query BM25, and Step2 only generates the query embedding for later reuse.

Usage example:
    from alicecore.modules.search.multi_vector import MultiSearcher, MultiConfig

    config = MultiConfig(multi_top_k=20, similarity_threshold=0.4)
    searcher = MultiSearcherES()
    results = await searcher.search(
        query="the Haier group's rendanheyi model",
        source_config_ids=["source_1", "source_2"],
        config=config,
    )
"""

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from alicecore.core.ai.factory import create_llm_client, get_embedding_client
from alicecore.core.ai.models import LLMMessage, LLMRole
from alicecore.core.storage.client import get_es_client
from alicecore.core.storage.repositories.entity_repository import EntityVectorRepository
from alicecore.core.storage.repositories.event_repository import EventVectorRepository
from alicecore.core.storage.repositories.event_entity_repository import EventEntityRepository
from alicecore.core.storage.repositories.source_chunk_repository import SourceChunkRepository
from alicecore.db import SourceChunk, SourceEvent, get_session_factory
from alicecore.modules.search.config import MultiConfig
from alicecore.utils import get_logger

logger = get_logger("search.multi_es")

PRECISE_ENTITY_EVENT_TOP_K = 40

# -- The LLM filter prompt (local version: no thought_process, only the ID list) --

_RERANK_SYSTEM_PROMPT_LOCAL = """I will provide you with a set of relationship descriptions from a knowledge graph. \
Select exactly {top_k} relationships most useful for answering this multi-hop question.

Return JSON with only "useful_relations" (list of {top_k} index numbers, most useful first). \
Do not include reason, thought_process, explanations, or relation text."""

_RERANK_EXAMPLE_1_INPUT_LOCAL = """I will provide you with a set of relationship descriptions from a knowledge graph. \
Select exactly 5 relationships most useful for answering this multi-hop question.

Return JSON with only "useful_relations" (list of 5 index numbers, most useful first).

Question:
When did Lothair Ii's mother die?

Relationship descriptions:
[53] bertha married to theobald of arles
[54] bertha married to adalbert ii of tuscany
[42] lothair ii son of ermengarde of tours
[43] lothair ii married to teutberga
[41] lothair ii son of emperor lothair i
[60] lothair ii husband of waldrada
[67] waldrada was mistress of lothair ii
"""

_RERANK_EXAMPLE_1_OUTPUT_LOCAL = """{"useful_relations": ["42", "41", "43", "60", "67"]}"""

_RERANK_EXAMPLE_2_INPUT_LOCAL = """I will provide you with a set of relationship descriptions from a knowledge graph. \
Select exactly 5 relationships most useful for answering this multi-hop question.

Return JSON with only "useful_relations" (list of 5 index numbers, most useful first).

Question:
What country is the composer of "Erta Eterna" from?

Relationship descriptions:
[12] terra eterna composed by paulo flores
[15] paulo flores born in angola
[18] paulo flores genre is semba
[22] angola located in africa
[25] semba originated in angola
[30] paulo flores nationality angolan
"""
_RERANK_EXAMPLE_2_OUTPUT_LOCAL = """{"useful_relations": ["12", "15", "30", "22", "25"]}"""

_RERANK_EXAMPLE_3_INPUT_LOCAL = """I will provide you with a set of relationship descriptions from a knowledge graph. \
Select exactly 5 relationships most useful for answering this multi-hop question.

Return JSON with only "useful_relations" (list of 5 index numbers, most useful first).

Question:
Who is the director of the film that won the award also won by "The Hurt Locker"?

Relationship descriptions:
[5] the hurt locker won academy award best picture
[8] the hurt locker directed by kathryn bigelow
[12] moonlight won academy award best picture
[15] moonlight directed by barry jenkins
[20] la la land won golden globe best musical
[25] barry jenkins born in miami
"""  
_RERANK_EXAMPLE_3_OUTPUT_LOCAL = """{"useful_relations": ["5", "12", "15", "8", "25"]}"""

_RERANK_TEMPLATE_LOCAL = """Question:
{question}

Relationship descriptions:
{relations}
"""


@dataclass
class MultiSearchState:
    entity_ids: set[str] = field(default_factory=set)
    relation_ids: set[str] = field(default_factory=set)


class MultiSearcherES:
    """
    Multi-element event retriever (the ES-first version)

    Retrieves multi-element events (each holding >= 3 entities).

    Differences from MultiSearcher:
    - the data access layer is ES throughout, only Step8 stays on MySQL
    - fewer MySQL connections and JOIN queries
    - the event_vectors.entity_ids field gives the event->entity map directly
    - the event_entity_vectors index gives the reverse entity->event lookup
    """

    def __init__(self, config: Optional[MultiConfig] = None):
        self._llm_client = None
        self._embedding_client = None
        es_client = get_es_client()
        self._entity_repo = EntityVectorRepository(es_client)
        self._event_repo = EventVectorRepository(es_client)
        self._event_entity_repo = EventEntityRepository(es_client)
        self._chunk_repo = SourceChunkRepository(es_client)

    # -- Lazy initialisation ---------------------------------------

    def _resolve_search_mode(self, config: Optional[Any]) -> str:
        """
        Resolve the multi_vector search mode.

        fast:    fast mode, BM25 entity recall + a small expansion
        precise: precise mode, BM25 entity recall + LLM filtering

        Defaults to fast when unspecified.
        """
        default_mode = MultiConfig.model_fields["mode"].default
        raw_mode = getattr(config, "mode", default_mode) if config is not None else default_mode
        mode = str(raw_mode).strip().lower()
        if mode in ("precise", "fast"):
            return mode
            raise ValueError("multi_vector mode only accepts fast or precise")

    def _resolve_ranking_strategy(self, config: Optional[Any]) -> str:
        mode = self._resolve_search_mode(config)
        if mode == "fast":
            return "fast"
        return "coarse_llm"

    async def _get_llm_client(self):
        if self._llm_client is None:
            self._llm_client = await create_llm_client(scenario="search")
        return self._llm_client

    def _get_entity_repo(self) -> EntityVectorRepository:
        return self._entity_repo

    def _get_event_repo(self) -> EventVectorRepository:
        return self._event_repo

    def _get_event_entity_repo(self) -> EventEntityRepository:
        return self._event_entity_repo

    async def _get_embedding_client(self):
        if self._embedding_client is None:
            self._embedding_client = await get_embedding_client(scenario="general")
        return self._embedding_client

    async def warmup(self, config: Optional[MultiConfig] = None) -> None:
        config = config or MultiConfig()
        await self._get_embedding_client()
        mode = self._resolve_search_mode(config)
        ranking_strategy = self._resolve_ranking_strategy(config)
        if ranking_strategy == "coarse_llm":
            await self._get_llm_client()
        logger.info(f"[multi_vector] mode={mode}, entity_recall=bm25, ranking={ranking_strategy}")

    # -- Step1: BM25 entity recall ---------------------------------

    async def step1_retrieve_entities_bm25(
        self,
        query: str,
        source_config_ids: List[str],
        *,
        entity_top_k: int,
        state: MultiSearchState,
        timings: Optional[Dict[str, float]] = None,
    ) -> List[str]:
        """
        Step1: BM25 entity recall over entity_vectors.name using the full query.

        Only a top-k truncation happens here; key_similarity_threshold is not used. A raw BM25 score is only
        meaningful for ordering within one query.
        """
        t_es = time.perf_counter()
        hits = await self._entity_repo.search_by_query_bm25(
            query=query,
            source_config_ids=source_config_ids,
            size=entity_top_k,
        )
        if timings is not None:
            timings["step1_entity_bm25_es_search"] = time.perf_counter() - t_es

        entity_ids: List[str] = []
        seen: set = set()
        details_for_log: List[str] = []

        for rank, hit in enumerate(hits, start=1):
            eid = hit.get("entity_id", "")
            name = str(hit.get("name") or "")
            score = float(hit.get("_score", 0.0) or 0.0)
            kept = 0

            if eid and eid not in seen and len(entity_ids) < entity_top_k:
                seen.add(eid)
                entity_ids.append(eid)
                state.entity_ids.add(eid)
                kept = 1

            details_for_log.append(
                f"{rank}:{eid} name={name!r} score={score:.4f} kept={kept}"
            )

        logger.info(
            f"[entity.bm25] input=1, candidates={len(hits)}, output={len(entity_ids)}, "
            f"top_k={entity_top_k}"
        )
        logger.info(
            f"[entity.bm25.entities] details={'; '.join(details_for_log)}"
        )
        return entity_ids

    # -- Step3: dual-channel recall (ES-first) ---------------------

    async def step3_retrieve_events(
        self,
        query: str,
        source_config_ids: List[str],
        *,
        query_vector: List[float],
        multi_top_k: int,
        entity_event_top_k: Optional[int] = None,
        similarity_threshold: float,
        entity_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Step3: dual-channel recall + deduplicated merge

        Channel 1 (entity->event): a batched ES event_entity_vectors query
        Channel 2 (query->event): an ES event_vectors title_vector kNN

        The two channels are merged and deduplicated by event_id, returning only event_id and score.

        Args:
            query: the query text
            source_config_ids: source ID list
            entity_ids: the entity IDs Step2 found (optional, feeds channel 1)
            multi_top_k: the channel 2 query->event cap
            entity_event_top_k: the channel 1 entity->event cap; multi_top_k is reused when omitted
            similarity_threshold: the minimum channel 2 vector similarity

        Returns:
            [{"event_id": str, "score": float}, ...]
        """
        threshold = similarity_threshold
        entity_event_top_k = entity_event_top_k or multi_top_k

        merged: Dict[str, float] = {}

        if query_vector is None:
            raise RuntimeError("Step3 needs a query_vector; generate it in batch inside search() and reuse it")

        event_repo = self._event_repo
        channel_tasks = []
        entity_event_task_index: Optional[int] = None

        # -- Channel 1: entity -> event (ES event_entity_vectors) --
        if entity_ids:
            ee_repo = self._event_entity_repo
            entity_event_task_index = len(channel_tasks)
            channel_tasks.append(
                ee_repo.get_event_ids_by_entity_ids(
                    entity_ids=entity_ids,
                    source_config_ids=source_config_ids,
                    size=entity_event_top_k,
                )
            )

        # -- Channel 2: query -> event (ES event_vectors title_vector kNN) --
        query_event_task_index = len(channel_tasks)
        channel_tasks.append(
            event_repo.search_similar_by_content(
                query_vector=query_vector,
                k=multi_top_k * 3,
                source_config_ids=source_config_ids,
            )
        )

        channel_results = await asyncio.gather(*channel_tasks)

        if entity_event_task_index is not None:
            event_ids_from_entities = channel_results[entity_event_task_index]
            db_count = 0
            for eid in event_ids_from_entities:
                merged[eid] = 0.0
                db_count += 1
        else:
            db_count = 0

        es_results = channel_results[query_event_task_index]

        es_count = 0
        es_new_count = 0

        for hit in es_results:
            if es_count >= multi_top_k:
                break

            score = hit.get("_score", 0.0)
            if score < threshold:
                continue

            eid = hit.get("event_id", "")
            if not eid:
                continue

            if eid not in merged:
                es_new_count += 1
            merged[eid] = score
            es_count += 1

        items = [{"event_id": eid, "score": score} for eid, score in merged.items()]

        logger.info(
            f"[event.recall] entity_to_event={db_count}/{entity_event_top_k}, "
            f"query_to_event={es_new_count}/{multi_top_k}, "
            f"merged={len(items)}"
        )
        return items

    @staticmethod
    def _merge_fast_event_channels(
        event1: List[Dict[str, Any]],
        event2: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Merge the event1/entity channel and the event2/query channel, keeping the highest vector score and the channel origin."""
        merged: Dict[str, Dict[str, Any]] = {}

        def add_events(events: List[Dict[str, Any]], channel: str) -> None:
            for item in events:
                eid = item.get("event_id")
                if not eid:
                    continue
                score = float(item.get("score", 0.0) or 0.0)
                if eid not in merged:
                    merged[eid] = {
                        "event_id": eid,
                        "score": score,
                        "vector_score": score,
                        "channels": [channel],
                        "entity_channel_score": None,
                        "query_channel_score": None,
                    }
                elif score > float(merged[eid].get("vector_score", 0.0) or 0.0):
                    merged[eid]["score"] = score
                    merged[eid]["vector_score"] = score

                if channel not in merged[eid]["channels"]:
                    merged[eid]["channels"].append(channel)
                score_key = f"{channel}_channel_score"
                prev_score = merged[eid].get(score_key)
                if prev_score is None or score > float(prev_score):
                    merged[eid][score_key] = score

        add_events(event1, "entity")
        add_events(event2, "query")

        results = list(merged.values())
        results.sort(key=lambda item: item.get("vector_score", 0.0), reverse=True)
        return results

    @staticmethod
    def _score_fast_events_with_entity_boost(
        events: List[Dict[str, Any]],
        seed_entity_ids: List[str],
        event_entities: Dict[str, List[str]],
        config: MultiConfig,
    ) -> List[Dict[str, Any]]:
        """Used only to pick the seed events before expand: the vector score + whether a seed entity was hit + the dual-channel bonus."""
        if not events:
            return []

        vector_scores = [float(item.get("vector_score", item.get("score", 0.0)) or 0.0) for item in events]
        min_score = min(vector_scores)
        max_score = max(vector_scores)
        score_range = max_score - min_score
        seed_entity_set = set(seed_entity_ids)

        scored: List[Dict[str, Any]] = []
        for item in events:
            eid = item["event_id"]
            raw_vector_score = float(item.get("vector_score", item.get("score", 0.0)) or 0.0)
            if score_range > 1e-9:
                vector_score_norm = (raw_vector_score - min_score) / score_range
            else:
                vector_score_norm = 1.0

            matched_entity_ids = sorted(set(event_entities.get(eid) or []) & seed_entity_set)
            entity_hit_score = 1.0 if matched_entity_ids else 0.0
            channel_score = 1.0 if len(item.get("channels") or []) > 1 else 0.0
            final_score = (
                config.fast_vector_weight * vector_score_norm
                + config.fast_entity_weight * entity_hit_score
                + config.fast_channel_weight * channel_score
            )

            scored_item = dict(item)
            scored_item["score"] = final_score
            scored_item["seed_score"] = final_score
            scored_item["vector_score_norm"] = vector_score_norm
            scored_item["entity_hit_score"] = entity_hit_score
            scored_item["entity_hit_count"] = len(matched_entity_ids)
            scored_item["matched_entity_ids"] = matched_entity_ids
            scored_item["channel_score"] = channel_score
            scored.append(scored_item)

        scored.sort(key=lambda item: item.get("score", 0.0), reverse=True)
        return scored

    @staticmethod
    def _merge_fast_and_expanded_for_final_rank(
        seed_items: List[Dict[str, Any]],
        expanded_items: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Merge the first-hop seed events and the expanded events, then order them all by query-content similarity.

        In fast mode the first hop's seed_score only picks the first-hop events; the final display order depends solely on
        final_similarity_score, so the entity boost cannot disturb the final chunk order.
        """
        final_items: List[Dict[str, Any]] = []
        seen_event_ids: set = set()
        for source_name, source_items in (
            ("seed", seed_items),
            ("expanded", expanded_items),
        ):
            for item in source_items:
                eid = item.get("event_id")
                if not eid or eid in seen_event_ids:
                    continue
                seen_event_ids.add(eid)
                final_item = dict(item)
                final_similarity_score = float(
                    final_item.get("vector_score", final_item.get("score", 0.0)) or 0.0
                )
                final_item["final_similarity_score"] = final_similarity_score
                final_item["score"] = final_similarity_score
                final_item["fast_stage"] = source_name
                final_items.append(final_item)

        final_items.sort(
            key=lambda item: item.get("final_similarity_score", 0.0),
            reverse=True,
        )
        return final_items

    async def step3_fast_recall(
        self,
        query: str,
        source_config_ids: List[str],
        *,
        entity_ids: List[str],
        query_vector: List[float],
        config: MultiConfig,
        timings: Dict[str, float],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
        """
        fast Step3:
        1. key -> entity topN
        2. entity -> event candidates -> query-content similarity top20 (event1)
        3. query -> event top20 (event2)
        4. union(event1,event2) -> pick the top-5 seeds with the binary entity-hit boost
        """
        seed_entity_ids = entity_ids[: config.fast_entity_k]

        t0 = time.perf_counter()
        candidate_event_ids = []
        if seed_entity_ids:
            candidate_event_ids = await self._event_entity_repo.get_event_ids_by_entity_ids(
                entity_ids=seed_entity_ids,
                source_config_ids=source_config_ids,
                size=config.fast_entity_event_candidate_k,
            )
        timings["step3_seed_entity_to_event_candidates"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        event1 = await self.step6_coarse_rank(
            query=query,
            event_ids=candidate_event_ids,
            source_config_ids=source_config_ids,
            query_vector=query_vector,
            max_events=config.fast_entity_event_k,
        )
        timings["step3_seed_event1_rank"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        event2_hits = await self._event_repo.search_similar_by_content(
            query_vector=query_vector,
            k=config.fast_query_event_k * 3,
            source_config_ids=source_config_ids,
        )
        event2: List[Dict[str, Any]] = []
        for hit in event2_hits:
            if len(event2) >= config.fast_query_event_k:
                break
            score = float(hit.get("_score", 0.0) or 0.0)
            if score < config.similarity_threshold:
                continue
            eid = hit.get("event_id", "")
            if eid:
                event2.append({"event_id": eid, "score": score})
        timings["step3_seed_event2_query"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        merged = self._merge_fast_event_channels(event1, event2)
        timings["step3_seed_merge"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        merged_event_ids = [item["event_id"] for item in merged]
        merged_event_fields = await self.step4_fetch_events(
            merged_event_ids,
            source_includes=["entity_ids"],
            log_label="seed candidate event entities",
        )
        merged_event_entities = {
            eid: fields.get("entity_ids") or []
            for eid, fields in merged_event_fields.items()
            if fields.get("entity_ids")
        }
        timings["step3_seed_fetch_candidate_entities"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        scored = self._score_fast_events_with_entity_boost(
            events=merged,
            seed_entity_ids=seed_entity_ids,
            event_entities=merged_event_entities,
            config=config,
        )
        seed_items = scored[: config.fast_answer_k]
        timings["step3_seed_score"] = time.perf_counter() - t0

        logger.info(
            f"[event.seed] entities={len(seed_entity_ids)}/{len(entity_ids)}, "
            f"entity_candidates={len(candidate_event_ids)}, "
            f"event1={len(event1)}, event2={len(event2)}, "
            f"merged={len(merged)}, seed={len(seed_items)}, "
            f"entity_boosted={sum(1 for item in scored if item.get('entity_hit_score'))}"
        )
        return seed_items, {"event1": event1, "event2": event2, "merged": scored}

    # -- Step4: event entity / content query (ES-first) ------------

    async def step4_fetch_events(
        self,
        event_ids: List[str],
        source_includes: List[str],
        log_label: str = "event",
    ) -> Dict[str, Dict[str, Any]]:
        """
        Step4: read the event fields from ES event_vectors as needed.

        source_includes controls which fields come back:
        - the Step5 expansion stage only reads entity_ids
        - before the Step7 LLM selection only content is read

        Args:
            event_ids: the event ID list
            source_includes: the fields ES should return; event_id need not be listed
            log_label: the stage label used in the log

        Returns:
            {event_id: {field: value, ...}}
        """
        if not event_ids:
            return {}

        includes = ["event_id"]
        for field in source_includes:
            if field != "event_id" and field not in includes:
                includes.append(field)

        event_repo = self._event_repo
        events = await event_repo.get_events_by_ids(
            event_ids,
            source_includes=includes,
        )

        event_map: Dict[str, Dict[str, Any]] = {}
        event_entity_relations = 0

        for event in events:
            eid = event.get("event_id", "")
            if not eid:
                continue
            item = {
                field: event.get(field)
                for field in includes
                if field != "event_id"
            }
            entity_ids = item.get("entity_ids")
            if isinstance(entity_ids, list):
                event_entity_relations += len(entity_ids)
            event_map[eid] = item

        extra = ""
        if "entity_ids" in includes:
            extra = f", event_entity_relations={event_entity_relations}"

        logger.info(
            f"[event.fetch] label={log_label}, input_event_ids={len(event_ids)}, "
            f"found_events={len(event_map)}{extra}"
        )
        return event_map

    # -- Deduplication helper --------------------------------------

    def get_new_entity_ids(
        self,
        event_entities: Dict[str, List[str]],
        state: MultiSearchState,
    ) -> List[str]:
        """
        Find the entity IDs in event_entities that this search has not visited yet

        Used during expansion to spot new entities and decide whether another round is needed.

        Args:
            event_entities: {event_id: [entity_id, ...]}

        Returns:
            The new entity ID list (deduplicated)
        """
        new_ids: List[str] = []
        seen_ids = set()
        total = 0
        for entity_ids in event_entities.values():
            for eid in entity_ids:
                if not eid or eid in seen_ids:
                    continue
                seen_ids.add(eid)
                total += 1
                if eid not in state.entity_ids:
                    new_ids.append(eid)
        logger.info(
            f"[entity.dedupe] total={total}, "
            f"already_tracked={total - len(new_ids)}, "
            f"new={len(new_ids)}"
        )
        return new_ids

    # -- Step5: multi-hop expansion (ES-first) ---------------------

    async def step5_expand(
        self,
        event_entities: Dict[str, List[str]],
        *,
        max_hops: int,
        max_expand_events_per_hop: int,
        state: MultiSearchState,
        source_config_ids: Optional[List[str]] = None,
        timings: Optional[Dict[str, float]] = None,
    ) -> List[str]:
        """
        Step5: multi-hop expansion

        Logic:
          hop=0: entity_set = the Step2 entities, relation_set = the Step3 merged events
          hop=N: prev_hop_entities -> new entity_ids (not in entity_set)
                 new entity_ids -> new event_ids (not in relation_set)
                 update both sets, prev_hop_entities = the entities of this hop's new events

        The ES version:
        - entity → event: event_entity_vectors.get_event_ids_by_entity_ids()
        - event → detail + entities: event_vectors.get_events_by_ids()

        Args:
            event_entities: the {event_id: [entity_id, ...]} map Step4 returned
            source_config_ids: source ID list (optional)
            max_hops: the maximum hop count, which search() must pass explicitly
            max_expand_events_per_hop: how many events the entity->event recall returns per hop, which search() must pass explicitly

        Returns:
            expanded_event_ids: the new event IDs recalled across every expansion round
        """
        expanded_event_ids: List[str] = []

        # hop=0: initialise relation_set (entity_set was already filled by step2)
        state.relation_ids.update(event_entities.keys())

        if max_hops == 0:
            return expanded_event_ids

        # The previous hop's event_entities, used to spot new entity_ids each round
        prev_hop_entities = event_entities

        ee_repo = self._event_entity_repo
        for hop in range(max_hops):
            pre_events = len(state.relation_ids)
            pre_entities = len(state.entity_ids)

            # 1. Find the new entity_ids in the previous hop's events (not in entity_set)
            t_step = time.perf_counter()
            new_entity_ids = self.get_new_entity_ids(prev_hop_entities, state)
            if timings is not None:
                timings["step5_get_new_entities"] = (
                    timings.get("step5_get_new_entities", 0.0)
                    + time.perf_counter() - t_step
                )

            if not new_entity_ids:
                logger.info(
                    f"[event.expand] hop={hop+1}/{max_hops} "
                    f"no_new_entities tracked_entities={len(state.entity_ids)}"
                )
                break

            # 2. Add the new entity_ids to entity_set
            t_step = time.perf_counter()
            state.entity_ids.update(new_entity_ids)
            if timings is not None:
                timings["step5_update_entities"] = (
                    timings.get("step5_update_entities", 0.0)
                    + time.perf_counter() - t_step
                )

            logger.info(
                f"[event.expand] hop={hop+1}/{max_hops} "
                f"entities: {pre_entities} -> +{len(new_entity_ids)} new, "
                f"total={len(state.entity_ids)}"
            )

            # 3. New entity_ids -> query ES event_entity_vectors for new event_ids
            #    event_entity_vectors carries source_config_id, so no JOIN is needed
            t_step = time.perf_counter()
            all_new_event_ids = await ee_repo.get_event_ids_by_entity_ids(
                entity_ids=new_entity_ids,
                source_config_ids=source_config_ids,
                exclude_event_ids=list(state.relation_ids),
                size=max_expand_events_per_hop,
            )
            if timings is not None:
                timings["step5_entity_to_event"] = (
                    timings.get("step5_entity_to_event", 0.0)
                    + time.perf_counter() - t_step
                )

            new_event_ids = all_new_event_ids

            if not new_event_ids:
                logger.info(
                    f"[event.expand] hop={hop+1}/{max_hops} "
                    f"no_new_events tracked_events={len(state.relation_ids)}"
                )
                break

            expanded_event_ids.extend(new_event_ids)
            is_last_hop = hop == max_hops - 1

            if is_last_hop:
                # The last hop's event_ids only need to reach the Step6 coarse ranking, so their entity_ids are not looked up again.
                t_step = time.perf_counter()
                state.relation_ids.update(new_event_ids)
                if timings is not None:
                    timings["step5_update_state"] = (
                        timings.get("step5_update_state", 0.0)
                        + time.perf_counter() - t_step
                    )

                logger.info(
                    f"[event.expand] hop={hop+1}/{max_hops} done: "
                    f"events {pre_events} -> {len(state.relation_ids)} (+{len(new_event_ids)}), "
                    f"entities {pre_entities} -> {len(state.entity_ids)}, "
                    f"limit={max_expand_events_per_hop}, "
                    f"last_hop_skip_event_entities=True"
                )
                break

            # 4. Only a non-final hop reads the new event entities, which the next hop expands from
            t_step = time.perf_counter()
            hop_event_fields = await self.step4_fetch_events(
                new_event_ids,
                source_includes=["entity_ids"],
                log_label="event entities",
            )
            hop_entities = {
                eid: fields.get("entity_ids") or []
                for eid, fields in hop_event_fields.items()
                if fields.get("entity_ids")
            }
            if timings is not None:
                timings["step5_fetch_event_entities"] = (
                    timings.get("step5_fetch_event_entities", 0.0)
                    + time.perf_counter() - t_step
                )

            # 5. Add the new event_ids to relation_set
            t_step = time.perf_counter()
            state.relation_ids.update(new_event_ids)

            # 6. Keep this hop's result for the next one
            prev_hop_entities = hop_entities
            if timings is not None:
                timings["step5_update_state"] = (
                    timings.get("step5_update_state", 0.0)
                    + time.perf_counter() - t_step
                )

            logger.info(
                f"[event.expand] hop={hop+1}/{max_hops} done: "
                f"events {pre_events} -> {len(state.relation_ids)} (+{len(new_event_ids)}), "
                f"entities {pre_entities} -> {len(state.entity_ids)}, "
                f"limit={max_expand_events_per_hop}"
            )

        return expanded_event_ids

    # -- Step6: coarse ranking (ES) --------------------------------

    async def step6_coarse_rank(
        self,
        query: str,
        event_ids: List[str],
        *,
        query_vector: List[float],
        max_events: int,
        source_config_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Step6: coarse ranking

        One kNN search over ES event_vectors with the query vector, filtered by event_ids,
        returning at most max_events rows in descending similarity.

        Args:
            query: the query text
            event_ids: the event IDs to rank
            source_config_ids: source ID list (optional)
            max_events: how many to return at most, which search() must pass explicitly

        Returns:
            [{"event_id": str, "score": float}, ...] in descending similarity
        """
        if not event_ids:
            return []

        event_repo = self._event_repo
        results = await event_repo.search_similar_by_content(
            query_vector=query_vector,
            k=max_events,
            source_config_ids=source_config_ids,
            event_ids=event_ids,
        )

        scored = []
        for hit in results:
            eid = hit.get("event_id", "")
            score = hit.get("_score", 0.0)
            if eid:
                scored.append({"event_id": eid, "score": score})

        top_score_str = f"{scored[0]['score']:.4f}" if scored else "0"
        logger.info(
            f"[event.rank.coarse] input={len(event_ids)}, "
            f"returned={len(scored)}, "
            f"top_score={top_score_str}"
        )
        return scored

    # -- Step7: LLM selection --------------------------------------
    # (identical to multi.py)

    def _parse_llm_filter_response(
        self,
        useful_relations: List[str],
        valid_ids: set,
    ) -> List[str]:
        """Parse the useful_relations the LLM returned, deduplicating and validating.

        A bare index string ("12") or a bracketed one ("[12]") is accepted; the regex anchors the whole string with $ and
        rejects any leftover format carrying trailing text or noise (such as "[12] relation text",
        "12: reason ...", "12abc"), so relation text that merely starts with a digit is never mistaken for a valid index.
        """
        selected: List[str] = []
        for rel_id in useful_relations:
            match = re.match(r"^\[?(\d+)\]?$", str(rel_id).strip())
            if not match:
                continue
            cid = match.group(1)
            if cid in valid_ids and cid not in selected:
                selected.append(cid)
        return selected

    async def step7_llm_filter(
        self,
        query: str,
        items: List[Dict[str, Any]],
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Step7: the LLM selects the most relevant multi-element events

        The candidates are formatted as [id] content and a few-shot prompt asks the LLM to
        pick the top_k most relevant ones; the response is parsed and mapped back onto the raw data.

        Args:
            query: the query text
            items: the candidate events [{event_id, content, score}]
            top_k: how many the selection returns

        Returns:
            The filtered event list, in the order the LLM chose
        """
        if not items:
            return []

        top_k = min(top_k, len(items))

        # 1. Build the idx -> event_id map and format the relation text
        idx_to_event_id: Dict[str, str] = {}
        relation_lines: List[str] = []

        for i, item in enumerate(items):
            idx = str(i)
            idx_to_event_id[idx] = item["event_id"]
            text = item.get("content", "").strip()
            relation_lines.append(f"[{i}] {text}")

        relations_str = "\n".join(relation_lines)
        valid_ids = set(idx_to_event_id.keys())

        # 2. Build the messages: SYSTEM + 3 few-shot pairs + the final prompt
        system_prompt = _RERANK_SYSTEM_PROMPT_LOCAL.format(top_k=top_k)
        messages = [
            LLMMessage(role=LLMRole.SYSTEM, content=system_prompt),
            # few-shot 1
            LLMMessage(role=LLMRole.USER, content=_RERANK_EXAMPLE_1_INPUT_LOCAL),
            LLMMessage(role=LLMRole.ASSISTANT, content=_RERANK_EXAMPLE_1_OUTPUT_LOCAL),
            # few-shot 2
            LLMMessage(role=LLMRole.USER, content=_RERANK_EXAMPLE_2_INPUT_LOCAL),
            LLMMessage(role=LLMRole.ASSISTANT, content=_RERANK_EXAMPLE_2_OUTPUT_LOCAL),
            # few-shot 3
            LLMMessage(role=LLMRole.USER, content=_RERANK_EXAMPLE_3_INPUT_LOCAL),
            LLMMessage(role=LLMRole.ASSISTANT, content=_RERANK_EXAMPLE_3_OUTPUT_LOCAL),
            # The real query
            LLMMessage(
                role=LLMRole.USER,
                content=_RERANK_TEMPLATE_LOCAL.format(question=query, relations=relations_str),
            ),
        ]

        # 3. Call the LLM
        llm_client = self._llm_client
        if llm_client is None:
            raise RuntimeError("The LLM client is not initialised; call await searcher.warmup(config) first")

        response = await llm_client.chat_with_schema(
            messages,
            response_schema={
                "type": "object",
                "properties": {
                    "useful_relations": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["useful_relations"],
                "additionalProperties": False,
            },
        )

        # Print the full output
        logger.info(f"[event.filter.llm.raw] raw_response={response}")

        # 4. Parse, deduplicate and validate
        useful_relations = response.get("useful_relations", [])
        selected_indices = self._parse_llm_filter_response(
            useful_relations,
            valid_ids,
        )

        # 5. Map back onto the raw data
        results = []
        event_id_to_item = {item["event_id"]: item for item in items}
        for idx in selected_indices[:top_k]:
            event_id = idx_to_event_id.get(idx)
            if event_id and event_id in event_id_to_item:
                results.append(event_id_to_item[event_id])

        return results

    # -- Step8: chunk lookup (MySQL) -------------------------------

    async def step8_fetch_chunks(
        self,
        event_ids: List[str],
    ) -> Dict[str, Dict[str, str]]:
        """
        Step8: find the chunk related to an event_id (MySQL)

        The event_vectors index has no chunk_id field, so this step stays on MySQL.

        source_event.chunk_id -> read the source_chunk details

        Args:
            event_ids: the event ID list

        Returns:
            {event_id: {"chunk_id": str, "heading": str, "content": str}}
        """
        if not event_ids:
            return {}

        session_factory = get_session_factory()
        event_chunk_map: Dict[str, str] = {}
        result_map: Dict[str, Dict[str, str]] = {}

        async with session_factory() as session:
            # 1. Look up event -> chunk_id
            stmt = select(SourceEvent.id, SourceEvent.chunk_id).where(
                SourceEvent.id.in_(event_ids)
            )
            result = await session.execute(stmt)
            chunk_ids: set = set()
            for row in result.fetchall():
                eid, chunk_id = row[0], row[1]
                if chunk_id:
                    event_chunk_map[eid] = chunk_id
                    chunk_ids.add(chunk_id)

            if not chunk_ids:
                return {}

            # 2. Read the chunk details
            chunk_stmt = select(SourceChunk).where(SourceChunk.id.in_(chunk_ids))
            result = await session.execute(chunk_stmt)
            chunk_map: Dict[str, Dict[str, str]] = {}
            for chunk in result.scalars().all():
                chunk_map[chunk.id] = {
                    "chunk_id": chunk.id,
                    "source_id": chunk.source_id or "",
                    "source_config_id": chunk.source_config_id or "",
                    "heading": chunk.heading or "",
                    "content": chunk.content or "",
                    "rank": chunk.rank,
                }

            # 3. Map by event_id
            for eid, chunk_id in event_chunk_map.items():
                if chunk_id in chunk_map:
                    result_map[eid] = chunk_map[chunk_id]

        logger.info(
            f"[chunk.fetch] events={len(event_ids)} -> "
            f"chunk_ids={len(chunk_ids)}, matched={len(result_map)}"
        )
        return result_map

    async def search_fast(
        self,
        query: str,
        source_config_ids: List[str],
        *,
        entity_ids: List[str],
        query_vector: List[float],
        config: MultiConfig,
        state: MultiSearchState,
        timings: Dict[str, float],
        t_total: float,
    ) -> Dict[str, Any]:
        """
        The fast-mode flow:
        key->entity(5) -> entity-filtered event1(20 by query-content score)
        query->event2(20) -> seed top5 -> expand -> expanded top5
        -> the seed and expanded events are ordered together by query-content similarity -> chunks.
        """
        state.entity_ids = set(entity_ids[: config.fast_entity_k])

        t0 = time.perf_counter()
        seed_items, _ = await self.step3_fast_recall(
            query=query,
            source_config_ids=source_config_ids,
            entity_ids=entity_ids,
            query_vector=query_vector,
            config=config,
            timings=timings,
        )
        timings["step3_fast_recall"] = time.perf_counter() - t0

        seed_event_ids = [item["event_id"] for item in seed_items]
        if not seed_event_ids:
            timings["total"] = time.perf_counter() - t_total
            return {
                "items": [],
                "_timings": timings,
                "_query_vector": query_vector,
            }

        t0 = time.perf_counter()
        seed_event_fields = await self.step4_fetch_events(
            seed_event_ids,
            source_includes=["entity_ids"],
            log_label="seed event entities",
        )
        seed_event_entities = {
            eid: fields.get("entity_ids") or []
            for eid, fields in seed_event_fields.items()
            if fields.get("entity_ids")
        }
        timings["step4_fast_event_entities"] = time.perf_counter() - t0

        state.relation_ids.update(seed_event_ids)

        t0 = time.perf_counter()
        expanded_event_ids = await self.step5_expand(
            event_entities=seed_event_entities,
            source_config_ids=source_config_ids,
            max_hops=config.max_hops,
            max_expand_events_per_hop=config.max_expand_events_per_hop,
            state=state,
            timings=timings,
        )
        timings["step5_fast_expand"] = time.perf_counter() - t0

        expanded_items: List[Dict[str, Any]] = []
        if expanded_event_ids and config.fast_expand_answer_k > 0:
            t0 = time.perf_counter()
            expanded_items = await self.step6_coarse_rank(
                query=query,
                event_ids=expanded_event_ids,
                source_config_ids=source_config_ids,
                query_vector=query_vector,
                max_events=config.fast_expand_answer_k,
            )
            timings["step6_fast_expand_rank"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        final_items = self._merge_fast_and_expanded_for_final_rank(
            seed_items,
            expanded_items,
        )
        timings["step6_fast_final_rank"] = time.perf_counter() - t0
        rank_input_count = len(seed_items) + len(expanded_items)
        output_scores = [
            float(item.get("final_similarity_score", 0.0) or 0.0)
            for item in final_items
        ]
        min_output_score = min(output_scores) if output_scores else 0.0
        max_output_score = max(output_scores) if output_scores else 0.0
        logger.info(
            f"[fast.rank] input={rank_input_count}, output={len(final_items)}, "
            f"score_min={min_output_score:.4f}, score_max={max_output_score:.4f}"
        )

        t0 = time.perf_counter()
        filtered_event_ids = [item["event_id"] for item in final_items]
        chunk_map = await self.step8_fetch_chunks(filtered_event_ids)
        timings["step8_chunks"] = time.perf_counter() - t0

        deduped: List[Dict[str, Any]] = []
        seen_chunk_ids: set = set()
        for item in final_items:
            item["chunk"] = chunk_map.get(item["event_id"])
            chunk = item.get("chunk")
            if not chunk:
                deduped.append(item)
                continue
            cid = chunk.get("chunk_id")
            if cid and cid in seen_chunk_ids:
                continue
            if cid:
                seen_chunk_ids.add(cid)
            deduped.append(item)

        timings["total"] = time.perf_counter() - t_total
        logger.info(
            f"[fast.done] seed={len(seed_items)}, expanded_selected={len(expanded_items)}, "
            f"final_events={len(final_items)}, final_chunks={len(deduped)}"
        )
        return {
            "items": deduped,
            "_timings": timings,
            "_query_vector": query_vector,
        }

    # -- The main search interface ---------------------------------

    async def search(
        self,
        query: str,
        source_config_ids: List[str],
        config: Optional[MultiConfig] = None,
    ) -> Dict[str, Any]:
        """
        Search the multi-element events

        Args:
            query: the query text
            source_config_ids: source ID list
            config: the MultiConfig configuration

        Returns:
            {
                "items": [
                    {
                        "event_id": str,
                        "content": str,
                        "score": float,
                        "chunk": {"chunk_id": str, "heading": str, "content": str} or None,
                    }
                ],
                "_timings": {"total": float}
            }
        """
        config = config or MultiConfig()
        state = MultiSearchState()
        timings: Dict[str, float] = {}
        t_total = time.perf_counter()
        ranking_strategy = self._resolve_ranking_strategy(config)
        precise_entity_event_top_k = (
            PRECISE_ENTITY_EVENT_TOP_K
            if ranking_strategy == "coarse_llm"
            else None
        )

        logger.info(
            f"[multi_es.start] mode={config.mode}, ranking={ranking_strategy}, "
            f"entity_top_k={config.entity_top_k}, multi_top_k={config.multi_top_k}, "
            f"entity_event_top_k={precise_entity_event_top_k or config.fast_entity_event_candidate_k}"
        )

        # Step1: query -> entity BM25
        t0 = time.perf_counter()
        entity_ids = await self.step1_retrieve_entities_bm25(
            query=query,
            source_config_ids=source_config_ids,
            entity_top_k=config.entity_top_k,
            state=state,
            timings=timings,
        )
        timings["step1_entity_bm25"] = time.perf_counter() - t0

        # Generate the query embedding once and reuse it downstream.
        t0 = time.perf_counter()
        embedding_client = self._embedding_client
        if embedding_client is None:
            raise RuntimeError("Embedding client not initialized; call await searcher.warmup(config) first")
        query_vector = (await embedding_client.batch_generate([query]))[0]
        timings["step2_embedding"] = time.perf_counter() - t0

        if ranking_strategy == "fast":
            return await self.search_fast(
                query=query,
                source_config_ids=source_config_ids,
                entity_ids=entity_ids,
                query_vector=query_vector,
                config=config,
                state=state,
                timings=timings,
                t_total=t_total,
            )

        t0 = time.perf_counter()
        event_items = await self.step3_retrieve_events(
            query=query,
            source_config_ids=source_config_ids,
            entity_ids=entity_ids,
            query_vector=query_vector,
            multi_top_k=config.multi_top_k,
            entity_event_top_k=precise_entity_event_top_k,
            similarity_threshold=config.similarity_threshold,
        )
        timings["step3_dual_recall"] = time.perf_counter() - t0

        event_ids = [item["event_id"] for item in event_items]
        if not event_ids:
            timings["total"] = time.perf_counter() - t_total
            return {
                "items": [],
                "_timings": timings,
                "_query_vector": query_vector,
            }

        t0 = time.perf_counter()
        event_fields = await self.step4_fetch_events(
            event_ids,
            source_includes=["entity_ids"],
            log_label="event entities",
        )
        event_entities = {
            eid: fields.get("entity_ids") or []
            for eid, fields in event_fields.items()
            if fields.get("entity_ids")
        }
        timings["step4_event_entities"] = time.perf_counter() - t0

        # The initially recalled events go on the blocklist, so the expansion stage cannot re-add them.
        state.relation_ids.update(event_ids)

        t0 = time.perf_counter()
        expanded_event_ids = await self.step5_expand(
            event_entities=event_entities,
            source_config_ids=source_config_ids,
            max_hops=config.max_hops,
            max_expand_events_per_hop=config.max_expand_events_per_hop,
            state=state,
            timings=timings,
        )
        timings["step5_expand"] = time.perf_counter() - t0

        all_event_ids = set(event_ids)
        all_event_ids.update(expanded_event_ids)

        t0 = time.perf_counter()
        candidate_items = await self.step6_coarse_rank(
            query=query,
            event_ids=list(all_event_ids),
            source_config_ids=source_config_ids,
            query_vector=query_vector,
            max_events=config.max_events,
        )
        timings["step6_coarse_rank"] = time.perf_counter() - t0
        candidate_scores = [
            float(item.get("score", 0.0) or 0.0)
            for item in candidate_items
        ]
        min_candidate_score = min(candidate_scores) if candidate_scores else 0.0
        max_candidate_score = max(candidate_scores) if candidate_scores else 0.0
        logger.info(
            f"[event.candidates] initial={len(event_ids)}, expanded={len(expanded_event_ids)}, "
            f"input={len(all_event_ids)}, output={len(candidate_items)}, "
            f"score_min={min_candidate_score:.4f}, score_max={max_candidate_score:.4f}"
        )

        t_step7 = time.perf_counter()
        t0 = time.perf_counter()
        candidate_event_ids = [item["event_id"] for item in candidate_items]
        event_contents = await self.step4_fetch_events(
            candidate_event_ids,
            source_includes=["content"],
            log_label="candidate event content",
        )
        timings["step7_fetch_candidate_contents"] = time.perf_counter() - t0

        candidates = []
        for item in candidate_items:
            eid = item["event_id"]
            detail = event_contents.get(eid, {})
            candidates.append({
                "event_id": eid,
                "content": detail.get("content") or "",
                "score": item["score"],
            })

        t0 = time.perf_counter()
        items = await self.step7_llm_filter(
            query=query,
            items=candidates,
            top_k=config.max_sections,
        )
        timings["step7_llm_call"] = time.perf_counter() - t0
        timings["step7_llm_filter"] = time.perf_counter() - t_step7

        t0 = time.perf_counter()
        selected_event_ids = [item["event_id"] for item in items]
        chunk_map = await self.step8_fetch_chunks(selected_event_ids)
        timings["step8_chunks"] = time.perf_counter() - t0

        for item in items:
            item["chunk"] = chunk_map.get(item["event_id"])

        deduped: List[Dict[str, Any]] = []
        seen_chunk_ids: set = set()
        for item in items:
            chunk = item.get("chunk")
            if not chunk:
                deduped.append(item)
                continue
            cid = chunk.get("chunk_id")
            if cid and cid in seen_chunk_ids:
                continue
            if cid:
                seen_chunk_ids.add(cid)
            deduped.append(item)
        items = deduped

        logger.info(
            f"[precise.done] events={len(candidate_items)}, selected={len(items)}, "
            f"chunks={sum(1 for item in items if item.get('chunk'))}"
        )

        timings["total"] = time.perf_counter() - t_total
        return {
            "items": items,
            "_timings": timings,
            "_query_vector": query_vector,
        }

    # -- The section-returning compatibility interface -------------

    async def search_for_sections(
        self,
        query: str,
        source_config_ids: List[str],
        query_vector: Optional[List[float]] = None,
        config: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Multi-element event retrieval that returns a section list.

        Keeps the {"sections": [...], "_timings": {...}} shape the old search engine needs.

        Args:
            query: the query text
            source_config_ids: source ID list
            query_vector: an optional precomputed vector (unused for now)
            config: a MultiConfig or SearchConfig object

        Returns:
            {"sections": [...], "_timings": {...}}
        """
        t_total = time.perf_counter()
        multi_config = config if isinstance(config, MultiConfig) else MultiConfig()

        result = await self.search(query, source_config_ids, multi_config)
        timings = result.get("_timings", {}).copy()
        if "total" in timings:
            timings.pop("total")

        seen_chunk_ids: set = set()
        sections = []
        for i, item in enumerate(result.get("items", [])):
            chunk = item.get("chunk")
            if not chunk:
                continue
            chunk_id = chunk["chunk_id"]
            if chunk_id in seen_chunk_ids:
                continue
            seen_chunk_ids.add(chunk_id)
            sections.append({
                "chunk_id": chunk_id,
                "source_id": chunk["source_id"],
                "source_config_id": chunk["source_config_id"],
                "heading": chunk["heading"],
                "content": chunk["content"],
                "rank": chunk.get("rank", i),
                "score": item["score"],
                "weight": item["score"],
            })

        # Native top-up: when deduplication leaves fewer than max_sections, fill up with query->chunk.
        target = multi_config.max_sections
        ranking_strategy = self._resolve_ranking_strategy(multi_config)
        if ranking_strategy == "coarse_llm" and len(sections) < target:
            multi_count = len(sections)
            supplement = await self.search_chunks(
                query=query,
                source_config_ids=source_config_ids,
                config=multi_config,
                query_vector=query_vector or result.get("_query_vector"),
            )
            supplement_timings = supplement.get("_timings", {})
            if "total" in supplement_timings:
                timings["native_chunk_total"] = supplement_timings["total"]
            native_added = 0
            for sec in supplement.get("sections", []):
                if sec["chunk_id"] in seen_chunk_ids:
                    continue
                seen_chunk_ids.add(sec["chunk_id"])
                sections.append(sec)
                native_added += 1
                if len(sections) >= target:
                    break
            logger.info(
                f"[native.fill] multi={multi_count}, native=+{native_added}, "
                f"total={len(sections)}"
            )

        timings["total"] = time.perf_counter() - t_total

        return {
            "sections": sections[:target],
            "_timings": timings,
        }

    # Kept for the old pipelineEngine interface name; no model rerank happens inside.
    search_for_rerank = search_for_sections

    async def search_chunks(
        self,
        query: str,
        source_config_ids: List[str],
        config: Optional[MultiConfig] = None,
        query_vector: Optional[List[float]] = None,
    ) -> Dict[str, Any]:
        """
        Direct query->chunk vector search

        Skips entity extraction and multi-hop expansion and retrieves chunks with the query vector.
        Useful for simple cases, or as a supplementary channel of the Multi pipeline.

        Args:
            query: the query text
            source_config_ids: source ID list
            config: the MultiConfig configuration

        Returns:
            {"sections": [...], "_timings": {"total": float}}
        """
        config = config or MultiConfig()
        start_time = time.perf_counter()

        if query_vector is None:
            raise RuntimeError("search_chunks needs a query_vector; reuse the one generated in batch inside search()")

        es_results = await self._chunk_repo.search_similar_by_content(
            query_vector=query_vector,
            k=config.max_sections * 2,
            source_config_ids=source_config_ids,
        )

        sections = []
        for result in es_results:
            score = result.get("_score", 0.0)
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

        sections = sorted(sections, key=lambda x: x["score"], reverse=True)[
            : config.max_sections
        ]
        total_time = time.perf_counter() - start_time

        logger.info(
            f"[query.chunk] returned={len(sections)}, total_time={total_time:.3f}s"
        )

        return {
            "sections": sections,
            "_timings": {"total": total_time},
        }


MultiSearcher = MultiSearcherES
ESFirstMultiSearcher = MultiSearcherES

__all__ = ["MultiSearcherES", "MultiSearcher", "ESFirstMultiSearcher"]
