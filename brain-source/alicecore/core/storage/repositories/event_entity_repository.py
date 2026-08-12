"""
Entity-event relation vector repository

Provides the business query methods for entity-event relation vectors
"""

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from alicecore.core.storage.query import Q, Search

from alicecore.core.storage.repositories.base import BaseRepository


class EventEntityRepository(BaseRepository):
    """Event-entity relation vector repository"""

    INDEX_NAME = "event_entity_vectors"

    async def index_event_entity(
        self,
        association_id: str,
        event_id: str,
        entity_id: str,
        source_config_id: str,
        description: str,
        vector: List[float],
        is_delete: bool = False,
        **kwargs,
    ) -> str:
        """
        Index one event-entity association

        Args:
            association_id: association ID
            event_id: event ID
            entity_id: entity ID
            source_config_id: source ID
            description: association description
            vector: the vector
            is_delete: whether it is deleted
            **kwargs: other fields (created_time and so on)

        Returns:
            The document ID
        """
        document = {
            "event_id": event_id,
            "entity_id": entity_id,
            "source_config_id": source_config_id,
            "description": description,
            "vector": vector,
            "is_delete": is_delete,
            **kwargs,
        }

        # source_config_id is the routing key, so one source's data lands on one shard
        return await self.index_document(
            self.INDEX_NAME, association_id, document, routing=source_config_id
        )

    def _is_valid_vector(self, vector: List[float]) -> bool:
        """
        Validate the vector (no NaN or Inf)

        Args:
            vector: the vector to validate

        Returns:
            bool: whether the vector is valid
        """
        if not vector:
            return False

        try:
            np_array = np.array(vector, dtype=np.float32)
            return not (np.isnan(np_array).any() or np.isinf(np_array).any())
        except (ValueError, TypeError):
            return False

    async def search_similar_by_description(
        self,
        query_vector: List[float],
        k: int = 10,
        source_config_id: Optional[str] = None,
        source_config_ids: Optional[List[str]] = None,
        event_id: Optional[str] = None,
        event_ids: Optional[List[str]] = None,
        entity_id: Optional[str] = None,
        entity_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search similar event-entity associations by the description vector

        Args:
            query_vector: the query vector
            k: how many to return
            source_config_id: source ID (single, backwards compatible)
            source_config_ids: source ID list (multi-source search)
            event_id: event ID filter (single, backwards compatible)
            event_ids: event ID list (several events)
            entity_id: entity ID filter (single, backwards compatible)
            entity_ids: entity ID list (several entities)

        Returns:
            The similar association list
        """
        if not self._is_valid_vector(query_vector):
            raise ValueError("The query vector contains invalid values (NaN or Inf)")

        # Argument compatibility: the list wins, falling back to the single value
        if not source_config_ids and source_config_id:
            source_config_ids = [source_config_id]

        if not event_ids and event_id:
            event_ids = [event_id]

        if not entity_ids and entity_id:
            entity_ids = [entity_id]

        # Add the filter conditions
        filters = []
        if source_config_ids:
            # A single source uses a term query, several sources use a terms query
            if len(source_config_ids) == 1:
                filters.append(Q("term", source_config_id=source_config_ids[0]))
            else:
                filters.append(Q("terms", source_config_id=source_config_ids))

        if event_ids:
            # A single value uses a term query, several use a terms query
            if len(event_ids) == 1:
                filters.append(Q("term", event_id=event_ids[0]))
            else:
                filters.append(Q("terms", event_id=event_ids))

        if entity_ids:
            # A single value uses a term query, several use a terms query
            if len(entity_ids) == 1:
                filters.append(Q("term", entity_id=entity_ids[0]))
            else:
                filters.append(Q("terms", entity_id=entity_ids))

        # Query only the associations that are not deleted
        filters.append(Q("term", is_delete=False))

        # Build the filter
        filter_query = None
        if filters:
            filter_query = Q("bool", must=filters).to_dict()

        # source_config_id as the routing key improves query performance
        # Routing is only used for a single source; it is disabled for several, so the query can cross shards
        routing = source_config_ids[0] if source_config_ids and len(source_config_ids) == 1 else None

        # Use the vector_search method
        return await self.es_client.vector_search(
            index=self.INDEX_NAME,
            field="vector",
            vector=query_vector,
            size=k,
            filter_query=filter_query,
            routing=routing,
        )

    async def get_by_event(
        self, event_id: str, source_config_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get every entity association of an event

        Args:
            event_id: event ID
            source_config_id: source ID (optional)

        Returns:
            The association list
        """
        s = Search(using=self.es_client, index=self.INDEX_NAME)

        s = s.filter("term", event_id=event_id)
        s = s.filter("term", is_delete=False)

        if source_config_id:
            s = s.filter("term", source_config_id=source_config_id)

        s = s[:100]

        # Convert to a dictionary and execute
        search_dict = s.to_dict()
        # When source_config_id is given, routing improves query performance
        routing = source_config_id if source_config_id else None
        response = await self.es_client.search(
            index=self.INDEX_NAME,
            query=search_dict.get("query", {}),
            size=search_dict.get("size", 10),
            routing=routing
        )

        # Two return shapes are handled: a list (default) or a dict (the full response)
        associations = []

        if isinstance(response, list):
            # Shape 1: a plain document list (the ES client's default)
            for assoc_data in response:
                if isinstance(assoc_data, dict):
                    associations.append(assoc_data)

        elif isinstance(response, dict) and "hits" in response:
            # Shape 2: the full response
            hits = response["hits"].get("hits", [])
            for hit in hits:
                if isinstance(hit, dict) and "_source" in hit:
                    associations.append(hit["_source"])

        return associations

    async def get_by_entity(
        self, entity_id: str, source_config_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get every event association of an entity

        Args:
            entity_id: entity ID
            source_config_id: source ID (optional)

        Returns:
            The association list
        """
        s = Search(using=self.es_client, index=self.INDEX_NAME)

        s = s.filter("term", entity_id=entity_id)
        s = s.filter("term", is_delete=False)

        if source_config_id:
            s = s.filter("term", source_config_id=source_config_id)

        s = s[:100]

        # Convert to a dictionary and execute
        search_dict = s.to_dict()
        # When source_config_id is given, routing improves query performance
        routing = source_config_id if source_config_id else None
        response = await self.es_client.search(
            index=self.INDEX_NAME,
            query=search_dict.get("query", {}),
            size=search_dict.get("size", 10),
            routing=routing
        )

        # Two return shapes are handled: a list (default) or a dict (the full response)
        associations = []

        if isinstance(response, list):
            # Shape 1: a plain document list (the ES client's default)
            for assoc_data in response:
                if isinstance(assoc_data, dict):
                    associations.append(assoc_data)

        elif isinstance(response, dict) and "hits" in response:
            # Shape 2: the full response
            hits = response["hits"].get("hits", [])
            for hit in hits:
                if isinstance(hit, dict) and "_source" in hit:
                    associations.append(hit["_source"])

        return associations

    async def get_event_ids_by_entity_ids(
        self,
        entity_ids: List[str],
        source_config_ids: Optional[List[str]] = None,
        exclude_event_ids: Optional[List[str]] = None,
        size: int = 10000,
    ) -> List[str]:
        """
        Get the related event IDs in batch from an entity ID list, deduplicated by event_id.

        event_entity_vectors.source_config_id filters the source, so SourceEvent needs no join;
        collapse on event_id does the deduplication on the ES side.
        """
        if not entity_ids:
            return []

        filters = [
            {"terms": {"entity_id": entity_ids}},
            {"term": {"is_delete": False}},
        ]
        must_not = []
        if source_config_ids:
            if len(source_config_ids) == 1:
                filters.append({"term": {"source_config_id": source_config_ids[0]}})
            else:
                filters.append({"terms": {"source_config_id": source_config_ids}})
        if exclude_event_ids:
            must_not.append({"terms": {"event_id": exclude_event_ids}})

        routing = (
            source_config_ids[0]
            if source_config_ids and len(source_config_ids) == 1
            else None
        )

        response = await self.es_client.search(
            index=self.INDEX_NAME,
            query={"bool": {"filter": filters, "must_not": must_not}},
            size=size,
            routing=routing,
            collapse={"field": "event_id"},
            _source=["event_id"],
        )

        event_ids: List[str] = []
        seen: set = set()

        if isinstance(response, list):
            for doc in response:
                eid = doc.get("event_id", "") if isinstance(doc, dict) else ""
                if eid and eid not in seen:
                    seen.add(eid)
                    event_ids.append(eid)
        elif isinstance(response, dict):
            hits = response.get("hits", {})
            if isinstance(hits, dict):
                hit_items = hits.get("hits", [])
            else:
                hit_items = hits
            for hit in hit_items:
                if not isinstance(hit, dict):
                    continue
                source = hit.get("_source") or hit.get("source") or {}
                eid = source.get("event_id", "")
                if not eid:
                    field_value = (hit.get("fields") or {}).get("event_id")
                    if isinstance(field_value, list) and field_value:
                        eid = field_value[0]
                    elif isinstance(field_value, str):
                        eid = field_value
                if eid and eid not in seen:
                    seen.add(eid)
                    event_ids.append(eid)

        return event_ids

    async def get_associations_by_ids(
        self, association_ids: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Get the associations in batch by association ID

        Args:
            association_ids: association ID list

        Returns:
            The detailed association list
        """
        if not association_ids:
            return []

        # Batched, so the ES max_result_window limit is never exceeded
        BATCH_SIZE = 3000
        results = []

        for i in range(0, len(association_ids), BATCH_SIZE):
            batch_ids = association_ids[i:i + BATCH_SIZE]

            query = {
                "terms": {
                    "_id": batch_ids
                }
            }

            response = await self.es_client.search(
                index=self.INDEX_NAME,
                query=query,
                size=len(batch_ids),
                return_full_response=True,
            )

            # With return_full_response=True the shape is {total, max_score, hits: [{id, score, source, index}]}
            for hit in response.get("hits", []):
                # The source field holds the actual document data
                source_data = hit.get("source", {})
                if source_data:
                    results.append(source_data.copy())

        return results

    async def batch_search_similar_by_event_entity_pairs(
        self,
        query_vector: List[float],
        event_entity_pairs: List[Tuple[str, str]],
        source_config_ids: Optional[List[str]] = None,
        include_source: bool = False,
    ) -> Dict[Tuple[str, str], float]:
        """
        Query the description vector similarity of (event_id, entity_id) pairs in batch

        Note: the describe text of one entity differs between events, so the query must be by pair

        Args:
            query_vector: the query vector
            event_entity_pairs: the (event_id, entity_id) pair list
            source_config_ids: source ID list
            include_source: whether to return the full source data (description and so on)

        Returns:
            include_source=False: {(event_id, entity_id): similarity_score}
            include_source=True: {(event_id, entity_id): {"score": float, "description": str, ...}}
        """
        if not event_entity_pairs:
            return {}

        if not self._is_valid_vector(query_vector):
            return {}

        # Batched, so the ES max_result_window limit is never exceeded
        BATCH_SIZE = 2500
        all_results = {}

        for i in range(0, len(event_entity_pairs), BATCH_SIZE):
            batch_pairs = event_entity_pairs[i:i + BATCH_SIZE]

            # Split event_ids and entity_ids for the terms queries
            batch_event_ids = [p[0] for p in batch_pairs]
            batch_entity_ids = [p[1] for p in batch_pairs]

            # Build the filter: event_id and entity_id must both match
            filters = [
                {"terms": {"event_id": batch_event_ids}},
                {"terms": {"entity_id": batch_entity_ids}},
                {"term": {"is_delete": False}}
            ]

            # Source filtering: a single source uses term + routing, several use terms
            routing = None
            if source_config_ids:
                if len(source_config_ids) == 1:
                    # Single source: a term query plus routing
                    filters.append({"term": {"source_config_id": source_config_ids[0]}})
                    routing = source_config_ids[0]
                else:
                    # Several sources: a terms query, no routing
                    filters.append({"terms": {"source_config_id": source_config_ids}})

            filter_query = {"bool": {"must": filters}}

            # Use vector_search; with a single source, routing improves query performance
            results = await self.es_client.vector_search(
                index=self.INDEX_NAME,
                field="vector",
                vector=query_vector,
                size=len(batch_pairs) * 2,
                filter_query=filter_query,
                routing=routing,
            )

            # Organise the results by (event_id, entity_id)
            for hit in results:
                event_id = hit.get("event_id")
                entity_id = hit.get("entity_id")
                score = hit.get("_score", 0.0)
                if event_id and entity_id:
                    if include_source:
                        # Return the full information
                        all_results[(event_id, entity_id)] = {
                            "score": float(score),
                            "description": hit.get("description", ""),
                            "source_config_id": hit.get("source_config_id"),
                            "is_delete": hit.get("is_delete", False),
                        }
                    else:
                        # Return only the score
                        all_results[(event_id, entity_id)] = float(score)

        return all_results
