"""
Entity vector repository

Provides the business query methods for entity vectors
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from alicecore.core.storage.query import Q, Search
from alicecore.core.storage.repositories.base import BaseRepository
from alicecore.db import get_session_factory


class EntityVectorRepository(BaseRepository):
    """Entity vector repository"""

    INDEX_NAME = "entity_vectors"
    
    # Class-level cache: type thresholds (cache key -> (thresholds_dict, timestamp))
    _type_thresholds_cache: Dict[str, Tuple[Dict[str, float], float]] = {}
    _CACHE_TTL_SECONDS = 300  # cached for 5 minutes

    def __init__(self, es_client: Any):
        """
        Initialise the repository

        Args:
            es_client: the vector backend client (method surface matching ElasticsearchClient)
        """
        super().__init__(es_client)
        self.session_factory = get_session_factory()

    async def index_entity(
        self,
        entity_id: str,
        source_config_id: str,
        entity_type: str,
        name: str,
        vector: List[float],
        **kwargs,
    ) -> str:
        """
        Index one entity

        Args:
            entity_id: entity ID
            source_config_id: source ID
            entity_type: entity type
            name: entity name
            vector: the vector
            **kwargs: other fields (created_time and so on)

        Returns:
            The document ID
        """
        document = {
            "entity_id": entity_id,
            "source_config_id": source_config_id,
            "type": entity_type,
            "name": name,
            "vector": vector,
            **kwargs,
        }

        # source_config_id is the routing key, so one source's data lands on one shard
        return await self.index_document(
            self.INDEX_NAME, entity_id, document, routing=source_config_id
        )

    async def search_by_name(
        self, name: str, source_config_id: Optional[str] = None, size: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Search entities by name

        Args:
            name: entity name (fuzzy match)
            source_config_id: source ID (optional)
            size: how many to return

        Returns:
            The entity list
        """
        s = Search(using=self.es_client, index=self.INDEX_NAME)

        # Build the query
        s = s.query("match", name=name)

        if source_config_id:
            s = s.filter("term", source_config_id=source_config_id)

        s = s[:size]

        # Convert to a dictionary and execute
        search_dict = s.to_dict()
        # When source_config_id is given, routing improves query performance
        routing = source_config_id if source_config_id else None
        response = await self.es_client.search(
            index=self.INDEX_NAME,
            query=search_dict.get("query", {}),
            size=search_dict.get("size", 10),
            routing=routing,
            return_full_response=True,
        )
        return [hit["source"] for hit in response.get("hits", [])]

    async def search_by_query_bm25(
        self,
        query: str,
        source_config_ids: Optional[List[str]] = None,
        size: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Search entities by matching the full query against entity names with BM25.

        The returned ``_score`` is the raw Elasticsearch BM25 score. It is useful
        for ordering within the same query, but it is not a global 0-1 similarity.
        """
        query_text = (query or "").strip()
        if not query_text or size <= 0:
            return []

        filter_clauses = []
        if source_config_ids:
            if len(source_config_ids) == 1:
                filter_clauses.append({"term": {"source_config_id": source_config_ids[0]}})
            else:
                filter_clauses.append({"terms": {"source_config_id": source_config_ids}})

        bool_query: Dict[str, Any] = {
            "must": [
                {
                    "match": {
                        "name": {
                            "query": query_text,
                            "operator": "or",
                        }
                    }
                }
            ]
        }
        if filter_clauses:
            bool_query["filter"] = filter_clauses

        routing = (
            source_config_ids[0]
            if source_config_ids and len(source_config_ids) == 1
            else None
        )

        response = await self.es_client.search(
            index=self.INDEX_NAME,
            query={"bool": bool_query},
            size=size,
            routing=routing,
            return_full_response=True,
            **{"_source": ["entity_id", "source_config_id", "type", "name"]},
        )

        results: List[Dict[str, Any]] = []
        for hit in response.get("hits", []):
            entity_data = (hit.get("source") or {}).copy()
            entity_data["_score"] = float(hit.get("score") or 0.0)
            results.append(entity_data)

        return results

    async def _get_entity_type_info(
        self, source_config_ids: Optional[List[str]] = None
    ) -> Dict[str, Dict[str, float]]:
        """
        Get the threshold and weight of each entity type (memory cached, TTL=5 minutes)

        Args:
            source_config_ids: source ID list (optional, multi-source supported)

        Returns:
            The entity type information: {type: {"threshold": 0.8, "weight": 1.5}}
        """
        # Build the cache key
        cache_key = ",".join(sorted(source_config_ids)) if source_config_ids else "__global__"
        
        # Check the cache
        now = time.time()
        if cache_key in self._type_thresholds_cache:
            cached_info, cached_time = self._type_thresholds_cache[cache_key]
            if now - cached_time < self._CACHE_TTL_SECONDS:
                return {k: v.copy() for k, v in cached_info.items()}  # deep copy
        
        # Cache miss, query the database
        from sqlalchemy import select
        from alicecore.db import EntityType

        type_info = {}

        async with self.session_factory() as session:
            query = select(EntityType.type, EntityType.similarity_threshold, EntityType.weight)

            if source_config_ids:
                # Look for the source-specific entity types, falling back to the system defaults
                query = query.where(
                    (EntityType.source_config_id.in_(source_config_ids)) | (EntityType.source_config_id.is_(None))
                )
            else:
                # Look only for the system default types
                query = query.where(EntityType.source_config_id.is_(None))

            query = query.where(EntityType.is_active == True)

            result = await session.execute(query)
            for entity_type, threshold, weight in result.fetchall():
                # When one type has several definitions, use the more specific (source-specific) one
                if entity_type not in type_info:
                    type_info[entity_type] = {
                        "threshold": float(threshold),
                        "weight": float(weight) if weight else 1.0
                    }

        # Update the cache
        self._type_thresholds_cache[cache_key] = (type_info, now)
        
        return type_info

    async def search_similar(
        self,
        query_vector: List[float],
        k: int = 10,
        source_config_id: Optional[str] = None,
        source_config_ids: Optional[List[str]] = None,
        entity_type: Optional[str] = None,
        include_type_threshold: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Vector similarity search

        Args:
            query_vector: the query vector
            k: how many to return
            source_config_id: source ID (single, backwards compatible)
            source_config_ids: source ID list (multi-source search)
            entity_type: entity type (optional)
            include_type_threshold: whether to include the entity type similarity threshold

        Returns:
            The similar entity list; with include_type_threshold=True it carries a type_threshold field
        """
        # Argument compatibility: source_config_ids wins, falling back to source_config_id
        if not source_config_ids and source_config_id:
            source_config_ids = [source_config_id]



        # Add the filter conditions
        filter_query = None
        filters = []
        if source_config_ids:
            # A single source uses a term query, several sources use a terms query
            if len(source_config_ids) == 1:
                filters.append(Q("term", source_config_id=source_config_ids[0]))
            else:
                filters.append(Q("terms", source_config_id=source_config_ids))
        if entity_type:
            filters.append(Q("term", type=entity_type))

        if filters:
            filter_query = Q("bool", must=filters).to_dict()

        # source_config_id as the routing key improves query performance
        # Routing is only used for a single source; it is disabled for several, so the query can cross shards
        routing = source_config_ids[0] if source_config_ids and len(source_config_ids) == 1 else None

        # Use the vector_search method
        search_results = await self.es_client.vector_search(
            index=self.INDEX_NAME,
            field="vector",
            vector=query_vector,
            size=k,
            filter_query=filter_query,
            routing=routing,
        )

        results = []
        if include_type_threshold:
            # Get the entity type information (threshold and weight)
            type_info = await self._get_entity_type_info(source_config_ids)

            for hit in search_results:
                entity_data = hit.copy()
                # Add the entity type's threshold and weight
                entity_type_name = entity_data.get("type")
                if entity_type_name and entity_type_name in type_info:
                    entity_data["type_threshold"] = type_info[entity_type_name]["threshold"]
                    entity_data["type_weight"] = type_info[entity_type_name]["weight"]
                else:
                    # Use the defaults
                    entity_data["type_threshold"] = 0.800
                    entity_data["type_weight"] = 1.0
                results.append(entity_data)
        else:
            results = search_results

        return results

    async def search_by_names_exact(
        self,
        names: List[str],
        source_config_ids: List[str],
        entity_types: Optional[List[str]] = None,
        size_per_name: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Search entities by an exact name match (batched)

        Args:
            names: entity name list (exact match)
            source_config_ids: source ID list
            entity_types: entity type list (optional, used as a filter)
            size_per_name: how many to return per name at most

        Returns:
            The entity list, with a _score field
        """
        if not names or not source_config_ids:
            return []

        # Build the bool query: names go into should (OR), source_config_ids into filter
        should_clauses = [{"term": {"name.keyword": name}} for name in names]

        filter_clauses = []
        # Add the source filter
        if len(source_config_ids) == 1:
            filter_clauses.append({"term": {"source_config_id": source_config_ids[0]}})
        else:
            filter_clauses.append({"terms": {"source_config_id": source_config_ids}})

        # Add the entity type filter (when given)
        if entity_types:
            if len(entity_types) == 1:
                filter_clauses.append({"term": {"type": entity_types[0]}})
            else:
                filter_clauses.append({"terms": {"type": entity_types}})

        query = {
            "bool": {
                "should": should_clauses,
                "minimum_should_match": 1,
                "filter": filter_clauses
            }
        }

        # Compute the overall size limit
        total_size = min(len(names) * size_per_name, 10000)

        # return_full_response=True gives the complete response
        response = await self.es_client.search(
            index=self.INDEX_NAME,
            query=query,
            size=total_size,
            return_full_response=True,
        )

        results = []
        for hit in response.get("hits", []):
            entity_data = hit.get("source", {}).copy()
            entity_data["_score"] = hit.get("score", 1.0)
            results.append(entity_data)

        return results

    async def get_by_source(
        self, source_config_id: str, entity_type: Optional[str] = None, size: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get every entity of a source

        Args:
            source_config_id: source ID
            entity_type: entity type (optional)
            size: how many to return

        Returns:
            The entity list
        """
        s = Search(using=self.es_client, index=self.INDEX_NAME)

        s = s.filter("term", source_config_id=source_config_id)

        if entity_type:
            s = s.filter("term", type=entity_type)

        s = s[:size]

        # Convert to a dictionary and execute
        search_dict = s.to_dict()
        # Routing improves query performance
        response = await self.es_client.search(
            index=self.INDEX_NAME,
            query=search_dict.get("query", {}),
            size=search_dict.get("size", 10),
            routing=source_config_id,
            return_full_response=True,
        )
        return [hit["source"] for hit in response.get("hits", [])]

    async def get_entities_by_ids(
        self, entity_ids: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Get entity information (vectors included) in batch by entity ID

        Args:
            entity_ids: entity ID list

        Returns:
            The detailed entity list, with entity_id, name, type and vector fields
        """
        if not entity_ids:
            return []

        # Elasticsearch defaults max_result_window to 10,000
        # Request in batches so the limit is never exceeded
        BATCH_SIZE = 5000  # a conservative batch size
        results = []

        for i in range(0, len(entity_ids), BATCH_SIZE):
            batch_ids = entity_ids[i:i + BATCH_SIZE]

            # Build the ES query
            query = {
                "terms": {
                    "entity_id": batch_ids
                }
            }

            response = await self.es_client.search(
                index=self.INDEX_NAME,
                query=query,
                size=len(batch_ids),
                return_full_response=True,
            )

            for hit in response.get("hits", []):
                entity_data = hit.get("source", {}).copy()
                results.append(entity_data)

        return results

    async def batch_search_similar_by_ids(
        self,
        query_vector: List[float],
        entity_ids: List[str],
        source_config_ids: Optional[List[str]] = None,
    ) -> Dict[str, float]:
        """
        Query the vector similarity of the given entities in batch

        Args:
            query_vector: the query vector
            entity_ids: entity ID list
            source_config_ids: source ID list

        Returns:
            {entity_id: similarity_score}
        """
        if not entity_ids:
            return {}

        # Batched, so the ES max_result_window limit is never exceeded
        BATCH_SIZE = 5000
        all_scores = {}

        for i in range(0, len(entity_ids), BATCH_SIZE):
            batch_ids = entity_ids[i:i + BATCH_SIZE]

            # Build the filter conditions
            filters = [{"terms": {"entity_id": batch_ids}}]
            if source_config_ids:
                filters.append({"terms": {"source_config_id": source_config_ids}})

            filter_query = {"bool": {"must": filters}} if filters else None

            # Use vector_search
            results = await self.es_client.vector_search(
                index=self.INDEX_NAME,
                field="vector",
                vector=query_vector,
                size=len(batch_ids),
                filter_query=filter_query,
            )

            for hit in results:
                entity_id = hit.get("entity_id")
                score = hit.get("_score", 0.0)
                if entity_id:
                    all_scores[entity_id] = float(score)

        return all_scores
