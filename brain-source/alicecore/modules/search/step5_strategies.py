"""
Step5 expansion strategies

Three multi-hop expansion strategies, decoupled from MultiSearcher by a strategy pattern:

- MultiStep5Strategy:   single-stage fixed-hop expansion
- Multi1Step5Strategy:  two-stage expansion; stage B seeds from every hop1 event entity
- HopLLMStep5Strategy:  two-stage expansion; stage B seeds from the coarse-ranked event entities

The core difference (how stage B picks its seeds):
- Multi1: seed = every hop1 event entity (breadth first, wide coverage)
- HopLLM: seed = the entities of the top-N events after the Step6 coarse ranking of eventset (quality first, higher precision)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from alicecore.db import EventEntity, SourceEvent, get_session_factory

if TYPE_CHECKING:
    from alicecore.modules.search.multi import MultiSearcher
    from alicecore.modules.search.config import MultiConfig

logger = logging.getLogger("search.step5_strategies")


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class Step5Strategy(ABC):
    """Abstract base class of a Step5 multi-hop expansion strategy"""

    @abstractmethod
    async def expand(
        self,
        searcher: "MultiSearcher",
        event_entities: Dict[str, List[str]],
        source_config_ids: Optional[List[str]],
        config: Any,
        query: str = "",
    ) -> Dict[str, Any]:
        """
        Run the expansion strategy.

        Args:
            searcher:          the MultiSearcher instance (sharing _entity_ids / _relation_ids)
            event_entities:    the {event_id: [entity_id, ...]} map Step4 returned (the hop0 events' entities)
            source_config_ids: source ID list
            config:            the configuration object for this strategy (MultiConfig)
            query:             the original query text (needed when HopLLM coarse-ranks its stage B seeds)

        Returns:
            {
                "eventset_details":  {event_id: {"title": str, "content": str}},  # hop0+hop1
                "eventset_entities": {event_id: [entity_id, ...]},                 # hop0+hop1
                "eventset1_details": {event_id: {"title": str, "content": str}},  # hop2+ (may be empty)
                "eventset1_entities":{event_id: [entity_id, ...]},                 # hop2+ (may be empty)
            }
        """

    # ------------------------------------------------------------------
    # Shared helpers (reused by the subclasses)
    # ------------------------------------------------------------------

    async def _query_new_event_ids(
        self,
        entity_ids: List[str],
        exclude_ids: set,
        source_config_ids: Optional[List[str]],
    ) -> List[str]:
        """
        Query the new event IDs related to entity_ids (excluding exclude_ids).

        Args:
            entity_ids:        the entity ID list
            exclude_ids:       the set of event IDs already seen (for deduplication)
            source_config_ids: the source ID filter

        Returns:
            The new event ID list
        """
        new_event_ids: List[str] = []
        session_factory = get_session_factory()
        async with session_factory() as session:
            stmt = select(EventEntity.event_id).where(
                EventEntity.entity_id.in_(entity_ids)
            ).distinct()
            if source_config_ids:
                stmt = stmt.join(
                    SourceEvent, SourceEvent.id == EventEntity.event_id
                ).where(
                    SourceEvent.source_config_id.in_(source_config_ids)
                )
            result = await session.execute(stmt)
            for row in result.fetchall():
                if row[0] not in exclude_ids:
                    new_event_ids.append(row[0])
        return new_event_ids


# ---------------------------------------------------------------------------
# Strategy one: MultiStep5Strategy (fixed-hop expansion)
# ---------------------------------------------------------------------------

class MultiStep5Strategy(Step5Strategy):
    """
    The multi strategy: single-stage fixed-hop expansion.

    Logic:
      hop=0: entity_set = the step2 entities, relation_set = the step3 merged events
      hop=N: the new entities of the previous hop's events -> new events (not in relation_set)
             both sets are updated until there is no new entity or event, or max_hops is reached
    """

    async def expand(
        self,
        searcher: "MultiSearcher",
        event_entities: Dict[str, List[str]],
        source_config_ids: Optional[List[str]],
        config: Any,
        query: str = "",
    ) -> Dict[str, Any]:
        max_hops = getattr(config, "max_hops", 1)

        all_details: Dict[str, Dict[str, str]] = {}
        all_entities: Dict[str, List[str]] = {}

        # hop=0: initialise relation_set (entity_set was already filled by step2)
        searcher._relation_ids.update(event_entities.keys())

        if max_hops == 0:
            return {
                "eventset_details": all_details,
                "eventset_entities": all_entities,
                "eventset1_details": {},
                "eventset1_entities": {},
            }

        prev_hop_entities = event_entities

        for hop in range(max_hops):
            pre_events = len(searcher._relation_ids)
            pre_entities = len(searcher._entity_ids)

            new_entity_ids = searcher.get_new_entity_ids(prev_hop_entities)
            if not new_entity_ids:
                logger.info(
                    f"[Step5-Multi] hop={hop+1}/{max_hops} "
                    f"no new entity (tracked_entities={len(searcher._entity_ids)}), stopping"
                )
                break

            searcher._entity_ids.update(new_entity_ids)
            logger.info(
                f"[Step5-Multi] hop={hop+1}/{max_hops} "
                f"entities: {pre_entities} -> +{len(new_entity_ids)} new, total={len(searcher._entity_ids)}"
            )

            new_event_ids = await self._query_new_event_ids(
                new_entity_ids, searcher._relation_ids, source_config_ids
            )

            if not new_event_ids:
                logger.info(
                    f"[Step5-Multi] hop={hop+1}/{max_hops} "
                    f"no new event (tracked_events={len(searcher._relation_ids)}), stopping"
                )
                break

            hop_details, hop_entities = await searcher.step4_fetch_event_details(new_event_ids)
            searcher._relation_ids.update(new_event_ids)
            all_details.update(hop_details)
            all_entities.update(hop_entities)
            prev_hop_entities = hop_entities

            logger.info(
                f"[Step5-Multi] hop={hop+1}/{max_hops} done: "
                f"events {pre_events} -> {len(searcher._relation_ids)} (+{len(new_event_ids)}), "
                f"entities {pre_entities} -> {len(searcher._entity_ids)}"
            )

        return {
            "eventset_details": all_details,
            "eventset_entities": all_entities,
            "eventset1_details": {},
            "eventset1_entities": {},
        }


# ---------------------------------------------------------------------------
# Strategy two: Multi1Step5Strategy (two stages - every seed)
# ---------------------------------------------------------------------------

class Multi1Step5Strategy(Step5Strategy):
    """
    The multi1 strategy: two-stage expansion; stage B seeds from every hop1 event entity.

    Stage A (one fixed hop): produces eventset (hop0 + hop1, deduplicated)
    Stage B (dynamic hops): keeps expanding from every hop1 event entity, producing eventset1 (hop2+, disjoint from eventset)
                       until len(eventset1) >= max_events_b or max_hop_retries is reached

    Falling short of max_events_b with the retries exhausted raises RuntimeError.
    """

    async def expand(
        self,
        searcher: "MultiSearcher",
        event_entities: Dict[str, List[str]],
        source_config_ids: Optional[List[str]],
        config: Any,
        query: str = "",
    ) -> Dict[str, Any]:
        max_events_b = getattr(config, "max_events_b", 0)
        max_hop_retries = getattr(config, "max_hop_retries", 3)

        # ==== Stage A: one fixed hop ====
        eventset_details, eventset_entities = await self._expand_phase_a(
            searcher, event_entities, source_config_ids
        )

        # ==== Stage B: dynamic hops seeded from every hop1 event entity ====
        # [key] every hop1 event entity is used (breadth first, no filtering)
        eventset1_details, eventset1_entities = await self._expand_phase_b(
            searcher=searcher,
            seed_event_entities=eventset_entities,  # every hop1 event entity
            source_config_ids=source_config_ids,
            max_events_b=max_events_b,
            max_hop_retries=max_hop_retries,
            raise_on_limit=False,  # over the limit only warns rather than raising, and the current result carries on
        )

        return {
            "eventset_details": eventset_details,
            "eventset_entities": eventset_entities,
            "eventset1_details": eventset1_details,
            "eventset1_entities": eventset1_entities,
        }

    async def _expand_phase_a(
        self,
        searcher: "MultiSearcher",
        event_entities: Dict[str, List[str]],
        source_config_ids: Optional[List[str]],
    ) -> Tuple[Dict[str, Dict[str, str]], Dict[str, List[str]]]:
        """Stage A: one fixed hop, producing eventset (hop0 + hop1)"""
        # hop=0: initialise relation_set
        searcher._relation_ids.update(event_entities.keys())

        eventset_details: Dict[str, Dict[str, str]] = {}
        eventset_entities: Dict[str, List[str]] = {}

        pre_events_a = len(searcher._relation_ids)
        pre_entities_a = len(searcher._entity_ids)

        new_entity_ids_hop1 = searcher.get_new_entity_ids(event_entities)

        if not new_entity_ids_hop1:
            logger.info(
                f"[Step5 stage A] hop=1 has no new entity (tracked_entities={len(searcher._entity_ids)}), "
                "stage A ends early and eventset holds hop0 only"
            )
            return eventset_details, eventset_entities

        searcher._entity_ids.update(new_entity_ids_hop1)

        hop1_event_ids = await self._query_new_event_ids(
            new_entity_ids_hop1, searcher._relation_ids, source_config_ids
        )

        if not hop1_event_ids:
            logger.info(
                f"[Step5 stage A] hop=1 has no new event (tracked_events={len(searcher._relation_ids)}), "
                "stage A ends early and eventset holds hop0 only"
            )
            return eventset_details, eventset_entities

        hop1_details, hop1_entities = await searcher.step4_fetch_event_details(hop1_event_ids)
        searcher._relation_ids.update(hop1_event_ids)
        eventset_details.update(hop1_details)
        eventset_entities.update(hop1_entities)

        logger.info(
            f"[Step5 stage A] hop=1 done: "
            f"events {pre_events_a} -> {len(searcher._relation_ids)} (+{len(hop1_event_ids)}), "
            f"entities {pre_entities_a} -> {len(searcher._entity_ids)}"
        )
        return eventset_details, eventset_entities

    async def _expand_phase_b(
        self,
        searcher: "MultiSearcher",
        seed_event_entities: Dict[str, List[str]],
        source_config_ids: Optional[List[str]],
        max_events_b: int,
        max_hop_retries: int,
        raise_on_limit: bool = False,
    ) -> Tuple[Dict[str, Dict[str, str]], Dict[str, List[str]]]:
        """
        Stage B: expand dynamically from the given event entities, producing eventset1 (hop2+).

        It is guaranteed disjoint from eventset (self._relation_ids).

        Args:
            seed_event_entities: the entity dictionary the expansion starts from
            raise_on_limit:      (kept for compatibility; neither strategy raises today) both True and False only warn and continue
        """
        eventset1_details: Dict[str, Dict[str, str]] = {}
        eventset1_entities: Dict[str, List[str]] = {}

        if not seed_event_entities:
            logger.info("[Step5 stage B] the seeds are empty, skipping stage B, eventset1 stays empty")
            return eventset1_details, eventset1_entities

        if max_events_b == 0:
            logger.info("[Step5 stage B] max_events_b=0, skipping stage B, eventset1 stays empty")
            return eventset1_details, eventset1_entities

        retry = 0
        hop_num = 2
        cur_hop_entities = seed_event_entities

        logger.info(
            f"[Step5 stage B] dynamic expansion started, target eventset1 >= {max_events_b}, "
            f"at most {max_hop_retries} hops"
        )

        while retry < max_hop_retries:
            pre_entities_b = len(searcher._entity_ids)

            logger.info(
                f"[Step5 stage B] -- hop={hop_num} (retry {retry+1}/{max_hop_retries}) started "
                f"| eventset1 now={len(eventset1_details)} / target={max_events_b} "
                f"| events carried from the previous hop={len(cur_hop_entities)}"
            )

            new_entity_ids = searcher.get_new_entity_ids(cur_hop_entities)

            if not new_entity_ids:
                logger.info(
                    f"[Step5 stage B] hop={hop_num} has no new entity (tracked entities={len(searcher._entity_ids)}), "
                    f"converged naturally, stopping ({retry} hops done, eventset1={len(eventset1_details)} rows)"
                )
                break

            logger.info(
                f"[Step5 stage B] hop={hop_num} found +{len(new_entity_ids)} new entities "
                f"(entity total {pre_entities_b} -> {pre_entities_b + len(new_entity_ids)})"
            )
            searcher._entity_ids.update(new_entity_ids)

            # Exclude the events already in eventset (_relation_ids) and in eventset1
            exclude = searcher._relation_ids | set(eventset1_details.keys())
            new_event_ids = await self._query_new_event_ids(
                new_entity_ids, exclude, source_config_ids
            )

            if not new_event_ids:
                logger.info(
                    f"[Step5 stage B] hop={hop_num} new entities relate to no new event, "
                    f"converged naturally, stopping ({retry} hops done, eventset1={len(eventset1_details)} rows)"
                )
                break

            hop_details, hop_entities = await searcher.step4_fetch_event_details(new_event_ids)
            # Note: stage B events are not added to _relation_ids (the eventset boundary stays clear)
            eventset1_details.update(hop_details)
            eventset1_entities.update(hop_entities)
            cur_hop_entities = hop_entities

            logger.info(
                f"[Step5 stage B] hop={hop_num} done "
                f"| +{len(new_event_ids)} events this hop "
                f"| eventset1 total {len(eventset1_details)} rows "
                f"| target {max_events_b}, "
                f"{'target met' if len(eventset1_details) >= max_events_b else f'{max_events_b - len(eventset1_details)} rows short'}"
            )

            if len(eventset1_details) >= max_events_b:
                logger.info(
                    f"[Step5 stage B] eventset1={len(eventset1_details)} >= max_events_b={max_events_b}, "
                    f"target met after {retry+1} hops (hop2~hop{hop_num}), stopping"
                )
                break

            retry += 1
            hop_num += 1

        else:
            # The while loop ended without a break, so the retry limit was reached while still short; only warn and continue
            msg = (
                f"[Step5 stage B] {max_hop_retries} expansion hops ran (hop2~hop{hop_num}), "
                f"and eventset1 is still short of max_events_b (now={len(eventset1_details)}, target={max_events_b})"
            )
            logger.warning(msg + "; the limit was reached, continuing with the current result")

        logger.info(
            f"[Step5 stage B] finished | eventset1 final={len(eventset1_details)} rows"
        )
        return eventset1_details, eventset1_entities


# ---------------------------------------------------------------------------
# Strategy three: HopLLMStep5Strategy (two stages - coarse-ranked seeds)
# ---------------------------------------------------------------------------

class HopLLMStep5Strategy(Step5Strategy):
    """
    The hopllm strategy: two-stage expansion; stage B seeds from the coarse-ranked event entities.

    Difference from Multi1:
    - Multi1: seed = every hop1 event entity (breadth first)
    - HopLLM: eventset is Step6 coarse-ranked first, and seed = the entities of the top events (quality first)

    Stage A (one fixed hop): produces eventset (hop0 + hop1)
    Intermediate step: coarse-rank eventset and take the entities of the top events as stage B's seeds
    Stage B (dynamic hops): expand from those selected seed entities, producing eventset1 (hop2+)

    Falling short of max_events_b with the retries exhausted only warns; it does not raise RuntimeError.
    """

    async def expand(
        self,
        searcher: "MultiSearcher",
        event_entities: Dict[str, List[str]],
        source_config_ids: Optional[List[str]],
        config: Any,
        query: str = "",
    ) -> Dict[str, Any]:
        max_events_a = getattr(config, "max_events_a", 100)
        max_events_b = getattr(config, "max_events_b", 0)
        max_hop_retries = getattr(config, "max_hop_retries", 3)

        # ==== Stage A: one fixed hop ====
        # Reuses the stage A logic of Multi1Step5Strategy (identical code)
        _multi1_strategy = Multi1Step5Strategy()
        eventset_details, eventset_entities = await _multi1_strategy._expand_phase_a(
            searcher, event_entities, source_config_ids
        )

        # eventset = hop0 (the step3 initial set) + hop1 (stage A)
        # The caller merges event_details and eventset_details here before passing them into step6
        # so the caller of searcher can obtain the complete eventset
        # Note: eventset_details holds only the hop1 additions; hop0 lives in event_details

        # ==== Intermediate step: coarse-rank eventset and pick the top events as stage B's seeds ====
        # The caller must supply the complete eventset (hop0+hop1), obtained through _relation_ids
        all_eventset_ids = list(searcher._relation_ids)

        logger.info(
            f"[Step5 HopLLM intermediate ranking] coarse-ranking eventset ({len(all_eventset_ids)} rows), "
            f"picking the top-{max_events_a} events as stage B's seeds..."
        )
        ranked_es = await searcher.step6_coarse_rank(
            query=query,
            event_ids=all_eventset_ids,
            source_config_ids=source_config_ids,
            max_events=max_events_a,
        )
        ranked_event_ids = [item["event_id"] for item in ranked_es]

        # Query the entities of the coarse-ranked events (a fresh DB query, because the hop0 entities are not in eventset_entities)
        logger.info(
            f"[Step5 HopLLM intermediate ranking] querying the entities of the {len(ranked_event_ids)} ranked events..."
        )
        _, seed_entities_for_b = await searcher.step4_fetch_event_details(ranked_event_ids)

        logger.info(
            f"[Step5 HopLLM intermediate ranking] obtained "
            f"{sum(len(v) for v in seed_entities_for_b.values())} seed entity relations"
        )

        # ==== Stage B: expand from the selected seed entities ====
        # [the key difference] the selected coarse-ranked entities are used, not every hop1 entity
        eventset1_details, eventset1_entities = await _multi1_strategy._expand_phase_b(
            searcher=searcher,
            seed_event_entities=seed_entities_for_b,  # the selected seeds
            source_config_ids=source_config_ids,
            max_events_b=max_events_b,
            max_hop_retries=max_hop_retries,
            raise_on_limit=False,  # over the limit hopllm only warns rather than raising
        )

        return {
            "eventset_details": eventset_details,
            "eventset_entities": eventset_entities,
            "eventset1_details": eventset1_details,
            "eventset1_entities": eventset1_entities,
        }


__all__ = [
    "Step5Strategy",
    "MultiStep5Strategy",
    "Multi1Step5Strategy",
    "HopLLMStep5Strategy",
]
