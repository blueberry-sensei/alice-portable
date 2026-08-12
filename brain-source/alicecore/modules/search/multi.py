"""
Multi-element event retriever

Merges roughly 88% of the duplicated code that used to live in multi.py / multi1.py / hopllm.py.

A strategy pattern handles the differences between the three Step5 expansion algorithms:
  - multi:   single stage, fixed hops (MultiStep5Strategy)
  - multi1:  two stages; stage B seeds from every hop1 entity (Multi1Step5Strategy)
  - hopllm:  two stages; stage B seeds from the coarse-ranked entities (HopLLMStep5Strategy)

Usage example:
    from alicecore.modules.search.multi import MultiSearcher
    from alicecore.modules.search.config import MultiConfig

    # the multi strategy
    searcher = MultiSearcher()
    results = await searcher.search(
        query="the Haier group's rendanheyi model",
        source_config_ids=["source_1"],
        config=MultiConfig(strategy="multi", multi_top_k=20, max_hops=2),
    )

    # the multi1 strategy
    results = await searcher.search(
        query="the Haier group's rendanheyi model",
        source_config_ids=["source_1"],
        config=MultiConfig(strategy="multi1", multi_top_k=20, max_events_a=100, max_events_b=50),
    )

    # the hopllm strategy
    results = await searcher.search(
        query="the Haier group's rendanheyi model",
        source_config_ids=["source_1"],
        config=MultiConfig(strategy="hopllm", multi_top_k=20, max_events_a=100, max_events_b=50),
    )
"""

import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from alicecore.core.ai.factory import create_llm_client
from alicecore.core.ai.models import LLMMessage, LLMRole
from alicecore.core.storage.client import get_es_client
from alicecore.core.storage.repositories.entity_repository import EntityVectorRepository
from alicecore.core.storage.repositories.event_repository import EventVectorRepository
from alicecore.core.storage.repositories.source_chunk_repository import SourceChunkRepository
from alicecore.db import EventEntity, SourceChunk, SourceEvent, get_session_factory
from alicecore.modules.load.processor import DocumentProcessor
from alicecore.modules.search.config import MultiConfig
from alicecore.modules.search.step5_strategies import (
    HopLLMStep5Strategy,
    Multi1Step5Strategy,
    MultiStep5Strategy,
    Step5Strategy,
)
from alicecore.utils import get_logger

logger = get_logger("search.multi")


@dataclass
class _SearchState:
    """Per-search mutable state used by Step5 expansion."""

    entity_ids: set = field(default_factory=set)
    relation_ids: set = field(default_factory=set)


_search_state_var: ContextVar[Optional[_SearchState]] = ContextVar(
    "multi_search_state",
    default=None,
)

# ---------------------------------------------------------------------------
# Shared prompt constants (identical in the three original files)
# ---------------------------------------------------------------------------

_NER_SYSTEM_PROMPT = "You're a very effective entity extraction system."

_NER_ONE_SHOT_INPUT = """Please extract all named entities that are important for solving the questions below.
Place the named entities in json format.

Question: Which magazine was started first Arthur's Magazine or First for Women?
"""

_NER_ONE_SHOT_OUTPUT = """{"named_entities": ["First for Women", "Arthur's Magazine"]}"""

_NER_TEMPLATE = "Question: {}"

_RERANK_SYSTEM_PROMPT = """I will provide you with a set of relationship descriptions from a knowledge graph. \
Select exactly {top_k} relationships most useful for answering this multi-hop question.

Return JSON with "thought_process" and "useful_relations" (list of {top_k} relation lines, most useful first)."""

_RERANK_EXAMPLE_1_INPUT = """I will provide you with a set of relationship descriptions from a knowledge graph. \
Select exactly 5 relationships most useful for answering this multi-hop question.

Return JSON with "thought_process" and "useful_relations" (list of 5 relation lines, most useful first).

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

_RERANK_EXAMPLE_1_OUTPUT = """{"thought_process": "2-hop question: First find Lothair II's mother (relation [42]: Ermengarde of Tours), \
then find death date. [41] gives father for family context.", \
"useful_relations": ["[42] lothair ii son of ermengarde of tours", "[41] lothair ii son of emperor lothair i", \
"[43] lothair ii married to teutberga", "[60] lothair ii husband of waldrada", "[67] waldrada was mistress of lothair ii"]}"""

_RERANK_EXAMPLE_2_INPUT = """I will provide you with a set of relationship descriptions from a knowledge graph. \
Select exactly 5 relationships most useful for answering this multi-hop question.

Return JSON with "thought_process" and "useful_relations" (list of 5 relation lines, most useful first).

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

_RERANK_EXAMPLE_2_OUTPUT = """{"thought_process": "2-hop question: First find composer of Terra Eterna ([12]: Paulo Flores), \
then find his country ([15] born in Angola or [30] nationality Angolan).", \
"useful_relations": ["[12] terra eterna composed by paulo flores", "[15] paulo flores born in angola", \
"[30] paulo flores nationality angolan", "[22] angola located in africa", "[25] semba originated in angola"]}"""

_RERANK_EXAMPLE_3_INPUT = """I will provide you with a set of relationship descriptions from a knowledge graph. \
Select exactly 5 relationships most useful for answering this multi-hop question.

Return JSON with "thought_process" and "useful_relations" (list of 5 relation lines, most useful first).

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

_RERANK_EXAMPLE_3_OUTPUT = """{"thought_process": "3-hop question: (1) Find award won by The Hurt Locker ([5]: Academy Award Best Picture), \
(2) Find another film with same award ([12]: Moonlight), (3) Find director ([15]: Barry Jenkins).", \
"useful_relations": ["[5] the hurt locker won academy award best picture", "[12] moonlight won academy award best picture", \
"[15] moonlight directed by barry jenkins", "[8] the hurt locker directed by kathryn bigelow", "[25] barry jenkins born in miami"]}"""

_RERANK_TEMPLATE = """Question:
{question}

Relationship descriptions:
{relations}
"""

# ---------------------------------------------------------------------------
# Multi-element event retriever
# ---------------------------------------------------------------------------

class MultiSearcher:
    """
    Multi-element event retriever

    A strategy pattern supports the three Step5 expansion algorithms:
    - strategy="multi"  -> MultiStep5Strategy  (fixed hops)
    - strategy="multi1" -> Multi1Step5Strategy (two stages, every seed)
    - strategy="hopllm" -> HopLLMStep5Strategy (two stages, coarse-ranked seeds)

    Every other step (Step1-4, Step6-8) is fully shared, with no duplication.
    """

    def __init__(self):
        self._llm_client = None
        self._processor = None
        self._entity_repo = None

    def _get_search_state(self) -> _SearchState:
        state = _search_state_var.get()
        if state is None:
            state = _SearchState()
            _search_state_var.set(state)
        return state

    @property
    def _entity_ids(self) -> set:
        return self._get_search_state().entity_ids

    @_entity_ids.setter
    def _entity_ids(self, value: set) -> None:
        self._get_search_state().entity_ids = value

    @property
    def _relation_ids(self) -> set:
        return self._get_search_state().relation_ids

    @_relation_ids.setter
    def _relation_ids(self, value: set) -> None:
        self._get_search_state().relation_ids = value

    # ------------------------------------------------------------------
    # Lazy-loading helpers (shared by Step1-2)
    # ------------------------------------------------------------------

    async def _get_llm_client(self):
        if self._llm_client is None:
            self._llm_client = await create_llm_client(scenario="search")
        return self._llm_client

    def _get_entity_repo(self) -> EntityVectorRepository:
        if self._entity_repo is None:
            self._entity_repo = EntityVectorRepository(get_es_client())
        return self._entity_repo

    async def _get_processor(self) -> DocumentProcessor:
        if self._processor is None:
            llm_client = await self._get_llm_client()
            self._processor = DocumentProcessor(llm_client=llm_client)
        return self._processor

    def _get_step5_strategy(self, config: MultiConfig) -> Step5Strategy:
        """Pick the Step5 strategy from the configured strategy field"""
        strategy = getattr(config, "strategy", "multi")
        if strategy == "multi":
            return MultiStep5Strategy()
        elif strategy == "multi1":
            return Multi1Step5Strategy()
        elif strategy == "hopllm":
            return HopLLMStep5Strategy()
        else:
            raise ValueError(f"Unsupported strategy: {strategy}, supported: multi, multi1, hopllm")

    # ------------------------------------------------------------------
    # Step1: NER entity extraction
    # ------------------------------------------------------------------

    async def step1_extract_entities(self, query: str) -> List[str]:
        """
        Step1: extract the named entities from the query

        Args:
            query: the user query text

        Returns:
            The entity name list, for example ["Haier group", "rendanheyi"]
        """
        llm_client = await self._get_llm_client()

        messages = [
            LLMMessage(role=LLMRole.SYSTEM, content=_NER_SYSTEM_PROMPT),
            LLMMessage(role=LLMRole.USER, content=_NER_ONE_SHOT_INPUT),
            LLMMessage(role=LLMRole.ASSISTANT, content=_NER_ONE_SHOT_OUTPUT),
            LLMMessage(role=LLMRole.USER, content=_NER_TEMPLATE.format(query)),
        ]

        response = await llm_client.chat_with_schema(
            messages,
            response_schema={
                "type": "object",
                "properties": {
                    "named_entities": {
                        "type": "array",
                        "items": {"type": "string"},
                    }
                },
                "required": ["named_entities"],
            },
        )

        entities = response.get("named_entities", response.get("entities", []))
        entities = [str(e).strip() for e in entities if e]

        logger.info(f"[Step1 entity extraction] query='{query}' -> entities={entities}")
        return entities

    # ------------------------------------------------------------------
    # Step2: entity vector search
    # ------------------------------------------------------------------

    async def step2_retrieve_entities(
        self,
        query_entities: List[str],
        source_config_ids: List[str],
        entity_top_k: Optional[int] = None,
        key_similarity_threshold: Optional[float] = None,
    ) -> Tuple[List[str], List[str], List[float]]:
        """
        Step2: search ES for entities similar to the names Step1 extracted

        Each query entity finds at most entity_top_k matches, and the similarity must be >= key_similarity_threshold.

        Args:
            query_entities: the entity name list Step1 extracted
            source_config_ids: source ID list
            entity_top_k: how many to retrieve per query entity (default 20)
            key_similarity_threshold: the minimum entity similarity (default 0.9)

        Returns:
            (entity_ids, entity_names, scores) as a deduplicated triple
        """
        if not query_entities:
            return [], [], []

        top_k = entity_top_k or 20
        threshold = key_similarity_threshold if key_similarity_threshold is not None else 0.9

        processor = await self._get_processor()
        repo = self._get_entity_repo()

        embeddings = [await processor.generate_embedding(name) for name in query_entities]

        entity_ids: List[str] = []
        entity_names: List[str] = []
        scores: List[float] = []
        seen: set = set()

        for vec in embeddings:
            results = await repo.search_similar(
                query_vector=vec,
                k=top_k,
                source_config_ids=source_config_ids,
            )
            for hit in results:
                score = hit.get("_score", 0.0)
                if score < threshold:
                    continue
                eid = hit.get("entity_id", "")
                if eid and eid not in seen:
                    seen.add(eid)
                    entity_ids.append(eid)
                    entity_names.append(hit.get("name", ""))
                    scores.append(score)
                    self._entity_ids.add(eid)

        logger.info(
            f"[Step2 entity search] query_entities={query_entities} -> "
            f"retrieved {len(entity_ids)} entities, "
            f"top_scores={scores[:5] if scores else []}"
        )
        return entity_ids, entity_names, scores

    # ------------------------------------------------------------------
    # Step3: dual-channel event recall
    # ------------------------------------------------------------------

    async def step3_retrieve_events(
        self,
        query: str,
        source_config_ids: List[str],
        entity_ids: Optional[List[str]] = None,
        multi_top_k: int = 20,
        similarity_threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Step3: dual-channel recall + deduplicated merge

        Channel 1 (entity->event): entity_ids -> EventEntity (unbounded)
        Channel 2 (query->event): the query embedding -> title_vector kNN (capped at multi_top_k)

        Args:
            query: the query text
            source_config_ids: source ID list
            entity_ids: the entity IDs Step2 found (optional, feeds channel 1)
            multi_top_k: the channel 2 query->event cap
            similarity_threshold: the minimum channel 2 vector similarity

        Returns:
            [{"event_id": str, "score": float}, ...]
        """
        threshold = similarity_threshold if similarity_threshold is not None else 0.4
        merged: Dict[str, float] = {}

        # Channel 1: entity -> event
        if entity_ids:
            session_factory = get_session_factory()
            async with session_factory() as session:
                stmt = select(EventEntity.event_id).where(
                    EventEntity.entity_id.in_(entity_ids)
                )
                if source_config_ids:
                    stmt = stmt.join(
                        SourceEvent, SourceEvent.id == EventEntity.event_id
                    ).where(
                        SourceEvent.source_config_id.in_(source_config_ids)
                    )
                result = await session.execute(stmt)
                for row in result.fetchall():
                    merged[row[0]] = 0.0

        # Channel 2: query -> event
        processor = await self._get_processor()
        query_vector = await processor.generate_embedding(query)
        event_repo = EventVectorRepository(get_es_client())
        es_results = await event_repo.search_similar_by_title(
            query_vector=query_vector,
            k=multi_top_k * 3,
            source_config_ids=source_config_ids,
        )

        es_new_count = 0
        es_count = 0
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

        db_count = len(merged) - es_new_count
        items = [{"event_id": eid, "score": score} for eid, score in merged.items()]
        logger.info(
            f"[Step3 dual-channel recall] query='{query}' -> "
            f"entity→event={db_count}, query→event={es_new_count}, "
            f"merged={len(items)}"
        )
        return items

    # ------------------------------------------------------------------
    # Step4: read the event details
    # ------------------------------------------------------------------

    async def step4_fetch_event_details(
        self,
        event_ids: List[str],
    ) -> Tuple[Dict[str, Dict[str, str]], Dict[str, List[str]]]:
        """
        Step4: read the event details and the related entities (read only, the Set is not updated)

        Args:
            event_ids: the event ID list

        Returns:
            (event_details, event_entities):
            - event_details:  {event_id: {"title": str, "content": str}}
            - event_entities: {event_id: [entity_id, ...]}
        """
        if not event_ids:
            return {}, {}

        session_factory = get_session_factory()
        event_details: Dict[str, Dict[str, str]] = {}
        event_entities: Dict[str, List[str]] = {}

        async with session_factory() as session:
            events_stmt = select(SourceEvent).where(SourceEvent.id.in_(event_ids))
            result = await session.execute(events_stmt)
            for event in result.scalars().all():
                event_details[event.id] = {
                    "title": event.title or "",
                    "content": event.content or "",
                }

            ee_stmt = select(EventEntity.event_id, EventEntity.entity_id).where(
                EventEntity.event_id.in_(event_ids)
            )
            result = await session.execute(ee_stmt)
            for row in result.fetchall():
                eid, kid = row[0], row[1]
                if eid not in event_details:
                    continue
                if eid not in event_entities:
                    event_entities[eid] = []
                event_entities[eid].append(kid)

        logger.info(
            f"[Step4 event details] input_event_ids={len(event_ids)}, "
            f"found_events={len(event_details)}, "
            f"event_entity_relations={sum(len(v) for v in event_entities.values())}"
        )
        return event_details, event_entities

    # ------------------------------------------------------------------
    # Entity deduplication helper
    # ------------------------------------------------------------------

    def get_new_entity_ids(self, event_entities: Dict[str, List[str]]) -> List[str]:
        """
        Find the entity IDs in event_entities that have not appeared in self._entity_ids

        Args:
            event_entities: {event_id: [entity_id, ...]}

        Returns:
            The new entity ID list (deduplicated)
        """
        all_ids = set()
        for entity_ids in event_entities.values():
            all_ids.update(entity_ids)
        new_ids = all_ids - self._entity_ids
        logger.info(
            f"[dedup] total={len(all_ids)}, "
            f"already_tracked={len(all_ids) - len(new_ids)}, "
            f"new={len(new_ids)}"
        )
        return list(new_ids)

    # ------------------------------------------------------------------
    # Step6: coarse ranking
    # ------------------------------------------------------------------

    async def step6_coarse_rank(
        self,
        query: str,
        event_ids: List[str],
        source_config_ids: Optional[List[str]] = None,
        max_events: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Step6: coarse ranking

        One kNN search in ES with the query vector, filtered by event_ids,
        returning at most max_events rows in descending similarity.

        Args:
            query: the query text
            event_ids: the event IDs to rank
            source_config_ids: source ID list (optional)
            max_events: how many to return at most (default 100)

        Returns:
            [{"event_id": str, "score": float}, ...] in descending similarity
        """
        if not event_ids:
            return []

        max_events = max_events or 100
        processor = await self._get_processor()
        query_vector = await processor.generate_embedding(query)

        event_repo = EventVectorRepository(get_es_client())
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
            f"[Step6 coarse ranking] input={len(event_ids)}, "
            f"returned={len(scored)}, "
            f"top_score={top_score_str}"
        )
        return scored

    # ------------------------------------------------------------------
    # Step7: LLM fine ranking
    # ------------------------------------------------------------------

    @staticmethod
    def _correct_rerank_line(
        predict_line: str,
        relation_texts: List[str],
        relation_ids: List[str],
    ) -> Optional[str]:
        """When the id the LLM returned is invalid, correct it by matching the text content"""
        text = predict_line[predict_line.find("]") + 1:].strip()
        for line_text, id_ in zip(relation_texts, relation_ids):
            if line_text.strip() == text:
                return id_
        return None

    def _parse_rerank_response(
        self,
        useful_relations: List[str],
        valid_ids: set,
        relation_ids: List[str],
        relation_texts: List[str],
    ) -> List[str]:
        """Parse the useful_relations the LLM returned, extract the [id] values and correct them"""
        selected: List[str] = []
        for line in useful_relations:
            line = str(line)
            if "[" not in line or "]" not in line:
                continue
            rel_id = line[line.find("[") + 1: line.find("]")].strip()
            if rel_id in valid_ids and rel_id not in selected:
                selected.append(rel_id)
            elif rel_id not in valid_ids:
                corrected = self._correct_rerank_line(line, relation_texts, relation_ids)
                if corrected and corrected not in selected:
                    selected.append(corrected)
        return selected

    async def step7_llm_rerank(
        self,
        query: str,
        items: List[Dict[str, Any]],
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Step7: the LLM selects the most relevant multi-element events

        Args:
            query: the query text
            items: the candidate events [{event_id, title, content, score}]
            top_k: how many the selection returns

        Returns:
            The filtered event list, in the order the LLM chose
        """
        if not items:
            return []

        top_k = min(top_k, len(items))

        idx_to_event_id: Dict[str, str] = {}
        relation_lines: List[str] = []
        relation_texts: List[str] = []

        for i, item in enumerate(items):
            idx = str(i)
            idx_to_event_id[idx] = item["event_id"]
            text = item.get("content", "").strip()
            relation_lines.append(f"[{i}] {text}")
            relation_texts.append(text)

        relations_str = "\n".join(relation_lines)
        valid_ids = set(idx_to_event_id.keys())

        system_prompt = _RERANK_SYSTEM_PROMPT.format(top_k=top_k)
        messages = [
            LLMMessage(role=LLMRole.SYSTEM, content=system_prompt),
            LLMMessage(role=LLMRole.USER, content=_RERANK_EXAMPLE_1_INPUT),
            LLMMessage(role=LLMRole.ASSISTANT, content=_RERANK_EXAMPLE_1_OUTPUT),
            LLMMessage(role=LLMRole.USER, content=_RERANK_EXAMPLE_2_INPUT),
            LLMMessage(role=LLMRole.ASSISTANT, content=_RERANK_EXAMPLE_2_OUTPUT),
            LLMMessage(role=LLMRole.USER, content=_RERANK_EXAMPLE_3_INPUT),
            LLMMessage(role=LLMRole.ASSISTANT, content=_RERANK_EXAMPLE_3_OUTPUT),
            LLMMessage(
                role=LLMRole.USER,
                content=_RERANK_TEMPLATE.format(question=query, relations=relations_str),
            ),
        ]

        llm_client = await self._get_llm_client()
        response = await llm_client.chat_with_schema(
            messages,
            response_schema={
                "type": "object",
                "properties": {
                    "thought_process": {"type": "string"},
                    "useful_relations": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["thought_process", "useful_relations"],
            },
        )

        useful_relations = response.get("useful_relations", [])
        if not useful_relations:
            logger.warning("The LLM returned no useful_relations, returning an empty list")
            return []

        selected_indices = self._parse_rerank_response(
            useful_relations, valid_ids, list(idx_to_event_id.keys()), relation_texts,
        )

        results = []
        event_id_to_item = {item["event_id"]: item for item in items}
        for idx in selected_indices[:top_k]:
            event_id = idx_to_event_id.get(idx)
            if event_id and event_id in event_id_to_item:
                results.append(event_id_to_item[event_id])

        logger.info(
            f"[Step7 LLM selection] query='{query}', candidates={len(items)}, "
            f"selected={len(results)}, top_k={top_k}"
        )
        return results

    # ------------------------------------------------------------------
    # Step8: read the chunks
    # ------------------------------------------------------------------

    async def step8_fetch_chunks(
        self,
        event_ids: List[str],
    ) -> Dict[str, Dict[str, str]]:
        """
        Step8: find the chunk related to an event_id

        source_event.chunk_id -> read the source_chunk details

        Args:
            event_ids: the event ID list

        Returns:
            {event_id: {"chunk_id": str, "heading": str, "content": str, ...}}
        """
        if not event_ids:
            return {}

        session_factory = get_session_factory()
        event_chunk_map: Dict[str, str] = {}
        result_map: Dict[str, Dict[str, str]] = {}

        async with session_factory() as session:
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

            for eid, chunk_id in event_chunk_map.items():
                if chunk_id in chunk_map:
                    result_map[eid] = chunk_map[chunk_id]

        logger.info(
            f"[Step8 chunk lookup] events={len(event_ids)} -> "
            f"chunk_ids={len(chunk_ids)}, matched={len(result_map)}"
        )
        return result_map

    # ------------------------------------------------------------------
    # Helper functions
    # ------------------------------------------------------------------

    @staticmethod
    def _build_candidates(
        ranked: List[Dict[str, Any]],
        details: Dict[str, Dict[str, str]],
    ) -> List[Dict[str, Any]]:
        """Assemble the candidate list from the coarse ranking and the event details"""
        candidates = []
        for item in ranked:
            eid = item["event_id"]
            detail = details.get(eid, {})
            candidates.append({
                "event_id": eid,
                "title": detail.get("title", ""),
                "content": detail.get("content", ""),
                "score": item["score"],
            })
        return candidates

    # ------------------------------------------------------------------
    # The main search method
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        source_config_ids: List[str],
        config: Optional[MultiConfig] = None,
    ) -> Dict[str, Any]:
        """
        The unified search entry point

        config.strategy picks the Step5 strategy automatically:
        - strategy="multi"  -> single-stage fixed-hop expansion
        - strategy="multi1" -> two-stage expansion (every seed)
        - strategy="hopllm" -> two-stage expansion (coarse-ranked seeds)

        Args:
            query: the query text
            source_config_ids: source ID list
            config: the configuration object (MultiConfig)

        Returns:
            {
                "items": [{"event_id", "title", "content", "score", "chunk"}, ...],
                "_timings": {"total": float},
            }
        """
        config = config or MultiConfig()
        strategy = self._get_step5_strategy(config)
        is_dual_phase = config.strategy in ("multi1", "hopllm")

        _search_state_var.set(_SearchState())
        start_time = time.perf_counter()

        logger.info(
            f"[multi-element retrieval - {config.strategy}] query='{query}', multi_top_k={config.multi_top_k}"
        )

        # ---- Step1: extract the entities ----
        logger.info("[Step1] LLM entity extraction...")
        t0 = time.perf_counter()
        query_entities = await self.step1_extract_entities(query)
        logger.info(f"[Step1] done ({time.perf_counter()-t0:.2f}s): {query_entities}")

        # ---- Step2: entity vector search ----
        logger.info("[Step2] embedding search over the entity vectors...")
        t0 = time.perf_counter()
        entity_ids, entity_names, entity_scores = await self.step2_retrieve_entities(
            query_entities=query_entities,
            source_config_ids=source_config_ids,
            entity_top_k=config.entity_top_k,
            key_similarity_threshold=config.key_similarity_threshold,
        )
        logger.info(f"[Step2] done ({time.perf_counter()-t0:.2f}s): {len(entity_ids)} entities")

        # ---- Step3: dual-channel recall ----
        logger.info("[Step3] dual-channel recall (entity->event + embedding query->event)...")
        t0 = time.perf_counter()
        event_items = await self.step3_retrieve_events(
            query=query,
            source_config_ids=source_config_ids,
            entity_ids=entity_ids,
            multi_top_k=config.multi_top_k,
            similarity_threshold=config.similarity_threshold,
        )
        logger.info(f"[Step3] done ({time.perf_counter()-t0:.2f}s): {len(event_items)} events")

        event_ids = [item["event_id"] for item in event_items]

        if not event_ids:
            total_time = time.perf_counter() - start_time
            logger.warning("[Step3] nothing was recalled, exiting early")
            return {"items": [], "_timings": {"total": total_time}}

        # ---- Step4: event details ----
        logger.info("[Step4] DB query for the event details...")
        t0 = time.perf_counter()
        event_details, event_entities = await self.step4_fetch_event_details(event_ids)
        logger.info(f"[Step4] done ({time.perf_counter()-t0:.2f}s): {len(event_details)} rows")

        # ---- Step5: multi-hop expansion (strategy pattern) ----
        if is_dual_phase:
            logger.info(
                f"[Step5] two-stage expansion "
                f"(max_events_a={getattr(config, 'max_events_a', '-')}, "
                f"max_events_b={getattr(config, 'max_events_b', '-')}, "
                f"max_hop_retries={getattr(config, 'max_hop_retries', '-')})..."
            )
        else:
            logger.info(f"[Step5] multi-hop expansion (max_hops={getattr(config, 'max_hops', '-')})...")

        t0 = time.perf_counter()
        expand_result = await strategy.expand(
            searcher=self,
            event_entities=event_entities,
            source_config_ids=source_config_ids,
            config=config,
            query=query,
        )
        eventset_details = expand_result["eventset_details"]
        eventset1_details = expand_result["eventset1_details"]

        logger.info(
            f"[Step5] done ({time.perf_counter()-t0:.2f}s): "
            f"eventset expansion={len(eventset_details)} rows"
            + (f", eventset1={len(eventset1_details)} rows" if is_dual_phase else "")
        )

        # ---- Step6 + Step7 + Step8 (the path forks on whether it is two-stage) ----
        if not is_dual_phase:
            # multi: single stage, merge every detail then coarse-rank and fine-rank once
            all_details = {**event_details, **eventset_details}

            logger.info(f"[Step6] embedding coarse ranking ({len(all_details)} candidates)...")
            t0 = time.perf_counter()
            ranked = await self.step6_coarse_rank(
                query=query,
                event_ids=list(all_details.keys()),
                source_config_ids=source_config_ids,
                max_events=getattr(config, "max_events", 100),
            )
            logger.info(f"[Step6] done ({time.perf_counter()-t0:.2f}s): {len(ranked)} rows")

            candidates = self._build_candidates(ranked, all_details)

        else:
            # multi1 / hopllm: two stages, eventset and eventset1 are coarse-ranked separately then merged
            all_eventset_details = {**event_details, **eventset_details}

            logger.info(
                f"[Step6-eventset] embedding coarse ranking ({len(all_eventset_details)} candidates)..."
            )
            t0 = time.perf_counter()
            ranked_es = await self.step6_coarse_rank(
                query=query,
                event_ids=list(all_eventset_details.keys()),
                source_config_ids=source_config_ids,
                max_events=getattr(config, "max_events_a", 100),
            )
            logger.info(
                f"[Step6-eventset] done ({time.perf_counter()-t0:.2f}s): {len(ranked_es)} rows"
            )
            candidates_es = self._build_candidates(ranked_es, all_eventset_details)

            candidates_es1: List[Dict[str, Any]] = []
            if eventset1_details:
                logger.info(
                    f"[Step6-eventset1] embedding coarse ranking ({len(eventset1_details)} candidates)..."
                )
                t0 = time.perf_counter()
                ranked_es1 = await self.step6_coarse_rank(
                    query=query,
                    event_ids=list(eventset1_details.keys()),
                    source_config_ids=source_config_ids,
                    max_events=getattr(config, "max_events_b", 100),
                )
                logger.info(
                    f"[Step6-eventset1] done ({time.perf_counter()-t0:.2f}s): {len(ranked_es1)} rows"
                )
                candidates_es1 = self._build_candidates(ranked_es1, eventset1_details)
            else:
                logger.info("[Step6-eventset1] eventset1 is empty, skipping the coarse ranking")

            candidates = candidates_es + candidates_es1
            logger.info(
                f"[Step7 merge] eventset={len(candidates_es)} rows + "
                f"eventset1={len(candidates_es1)} rows = {len(candidates)} rows in total"
            )

        # ---- Step7: LLM selection ----
        logger.info(f"[Step7] LLM selects top-{config.rerank_top_k} (candidates={len(candidates)})...")
        t0 = time.perf_counter()
        items = await self.step7_llm_rerank(
            query=query,
            items=candidates,
            top_k=config.rerank_top_k,
        )
        logger.info(f"[Step7] done ({time.perf_counter()-t0:.2f}s): {len(items)} rows selected")

        # ---- Step8: chunk lookup ----
        logger.info("[Step8] DB lookup for the related chunks...")
        t0 = time.perf_counter()
        chunk_map = await self.step8_fetch_chunks([i["event_id"] for i in items])
        logger.info(f"[Step8] done ({time.perf_counter()-t0:.2f}s): {len(chunk_map)} rows")

        for item in items:
            item["chunk"] = chunk_map.get(item["event_id"])

        total_time = time.perf_counter() - start_time
        logger.info(
            f"[multi-element retrieval - {config.strategy}] done total={total_time:.2f}s, items={len(items)}"
        )
        return {
            "items": items,
            "_timings": {"total": total_time},
        }

    # ------------------------------------------------------------------
    # Backwards compatible interface
    # ------------------------------------------------------------------

    async def search_for_rerank(
        self,
        query: str,
        source_config_ids: List[str],
        query_vector: Optional[List[float]] = None,
        config: Optional[MultiConfig] = None,
    ) -> Dict[str, Any]:
        """
        Multi-element event retrieval (the rerank-compatible interface)

        The return shape matches VectorSearcher.search_chunks_for_rerank,
        so it drops into the shared rerank flow.

        Returns:
            {"sections": [...], "_timings": {...}}
        """
        multi_config = config or MultiConfig()

        result = await self.search(query, source_config_ids, multi_config)

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

        target = multi_config.max_sections
        if len(sections) < target:
            multi_count = len(sections)
            supplement = await self.search_chunks(
                query=query,
                source_config_ids=source_config_ids,
                config=multi_config,
            )
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
                f"[native top-up] multi={multi_count}, native=+{native_added}, "
                f"total={len(sections)}"
            )

        return {
            "sections": sections[:target],
            "_timings": result.get("_timings", {}),
        }

    async def search_chunks(
        self,
        query: str,
        source_config_ids: List[str],
        config: Optional[MultiConfig] = None,
    ) -> Dict[str, Any]:
        """
        Direct query->chunk vector search

        Skips entity extraction and multi-hop expansion and retrieves chunks with the query vector.
        Useful for simple cases, or as a supplementary channel of the Multi pipeline.

        Returns:
            {"sections": [...], "_timings": {"total": float}}
        """
        config = config or MultiConfig()
        start_time = time.perf_counter()

        processor = await self._get_processor()
        query_vector = await processor.generate_embedding(query)

        chunk_repo = SourceChunkRepository(get_es_client())
        es_results = await chunk_repo.search_similar_by_content(
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

        sections = sorted(sections, key=lambda x: x["score"], reverse=True)[:config.max_sections]
        total_time = time.perf_counter() - start_time

        logger.info(
            f"[Query→Chunk] query='{query}', "
            f"returned={len(sections)}, total_time={total_time:.3f}s"
        )
        return {
            "sections": sections,
            "_timings": {"total": total_time},
        }


__all__ = ["MultiSearcher"]
