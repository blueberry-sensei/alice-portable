"""
Event vector repository

Provides the business query methods for event vectors
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
from alicecore.core.storage.query import Q, Search

from alicecore.core.storage.repositories.base import BaseRepository


class EventVectorRepository(BaseRepository):
    """Event vector repository"""

    INDEX_NAME = "event_vectors"

    async def index_event(
        self,
        event_id: str,
        source_config_id: str,
        source_type: str,
        source_id: str,
        title: str,
        summary: str,
        content: str,
        title_vector: List[float],
        content_vector: List[float],
        **kwargs,
    ) -> str:
        """
        Index one event

        Args:
            event_id: event ID
            source_config_id: source ID
            source_type: source type (ARTICLE/CHAT)
            source_id: source ID
            title: title
            summary: summary
            content: content
            title_vector: the title vector
            content_vector: the content vector
            **kwargs: other fields (category, tags, entity_ids, start_time, end_time and so on)

        Returns:
            The document ID
        """
        document = {
            "event_id": event_id,
            "source_config_id": source_config_id,
            "source_type": source_type,
            "source_id": source_id,
            "title": title,
            "summary": summary,
            "content": content,
            "title_vector": title_vector,
            "content_vector": content_vector,
            **kwargs,
        }

        # source_config_id is the routing key, so one source's data lands on one shard
        return await self.index_document(
            self.INDEX_NAME, event_id, document, routing=source_config_id
        )

    async def search_by_text(
        self, query: str, source_config_id: Optional[str] = None, size: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Full-text search

        Args:
            query: the query text
            source_config_id: source ID (optional)
            size: how many to return

        Returns:
            The event list
        """
        s = Search(using=self.es_client, index=self.INDEX_NAME)

        # Multi-field query
        s = s.query(
            "multi_match",
            query=query,
            fields=["title^3", "summary^2", "content"],  # title carries the highest weight
        )

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
            routing=routing
        )
        return response

    async def get_event_vector(
        self, event_id: str, vector_type: str = "content_vector"
    ) -> Optional[List[float]]:
        """
        Get the vector of one event

        Args:
            event_id: event ID
            vector_type: vector type, "title_vector" or "content_vector"

        Returns:
            The event vector, or None when it does not exist
        """
        # Build the ES query
        query_body = {
            "query": {
                "term": {
                    "event_id": event_id
                }
            },
            "size": 1,
            "_source": [vector_type, "event_id", "title", "summary"]
        }

        try:
            response = await self.es_client.search(
                index=self.INDEX_NAME,
                query=query_body["query"],
                size=query_body.get("size", 1),
                return_full_response=True,
                **{"_source": query_body.get("_source", [])}
            )

            hits = response.get("hits", [])

            if hits:
                # Handle different response formats
                if isinstance(hits[0], dict):
                    # Standard format: hit is a dict with _source field
                    event_data = hits[0].get(
                        "source", hits[0].get("_source", {}))
                else:
                    # Non-standard format: hit might be an object with attributes
                    event_data = getattr(
                        hits[0], "source", getattr(hits[0], "_source", {}))

                return event_data.get(vector_type)
            else:
                return None

        except Exception as e:
            # On a failed query return None rather than raising
            return None

    async def get_events_by_ids(
        self,
        event_ids: List[str],
        source_includes: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get the detailed event information from an event ID list

        Args:
            event_ids: event ID list
            source_includes: optional _source allowlist; the old behaviour is kept when omitted

        Returns:
            The detailed event list
        """
        if not event_ids:
            return []

        source_filter = (
            {"includes": source_includes}
            if source_includes
            else {"excludes": ["title_vector"]}
        )

        # The old behaviour is the default; a caller may pass source_includes to move less data.
        query_body = {
            "query": {
                "terms": {
                    "event_id": event_ids
                }
            },
            "size": len(event_ids),
            "_source": source_filter
        }

        try:
            response = await self.es_client.search(
                index=self.INDEX_NAME,
                query=query_body["query"],
                size=query_body.get("size", 10),
                **{"_source": query_body.get("_source")}
            )

            # Make sure the data shape is right
            events = []

            # Two return shapes are handled:
            # 1. dict (the full response): {"hits": {"hits": [{"_source": {...}, "_id": "..."}]}}
            # 2. list (a document list): [{event_id: "...", ...}, ...]

            if isinstance(response, list):
                # Shape 2: a plain document list (the ES client's default)
                for event_data in response:
                    if isinstance(event_data, dict) and "event_id" in event_data:
                        events.append(event_data)
                    else:
                        print(f"Warning: the event data has no event_id field: {event_data}")

            elif isinstance(response, dict) and "hits" in response:
                # Shape 1: the full response
                hits = response["hits"].get("hits", [])
                for hit in hits:
                    if isinstance(hit, dict):
                        # Prefer the _source field
                        if "_source" in hit:
                            event_data = hit["_source"]
                        elif "source" in hit:
                            event_data = hit["source"]
                        else:
                            continue

                        # Make sure the event_id field exists
                        if "event_id" not in event_data and "_id" in hit:
                            event_data["event_id"] = hit["_id"]

                        # Validate the required fields
                        if isinstance(event_data, dict) and "event_id" in event_data:
                            events.append(event_data)
                        else:
                            print(f"Warning: the event data has no event_id field: {event_data}")
            else:
                print(f"Warning: unexpected Elasticsearch response shape: {type(response)}")

            return events

        except Exception as e:
            print(f"Event query failed: {e}")
            return []

    async def search_similar_by_title(
        self,
        query_vector: List[float],
        k: int = 10,
        source_config_id: Optional[str] = None,
        source_config_ids: Optional[List[str]] = None,
        category: Optional[str] = None,
        event_ids: Optional[List[str]] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search similar events by the title vector

        Args:
            query_vector: the query vector
            k: how many to return
            source_config_id: source ID (single, backwards compatible)
            source_config_ids: source ID list (multi-source search)
            category: category (optional)
            start_time: start of the time range (optional)
            end_time: end of the time range (optional)

        Returns:
            The similar event list
        """
        return await self._vector_search(
            "title_vector",
            query_vector,
            k,
            source_config_id,
            source_config_ids,
            category,
            event_ids,
            start_time,
            end_time,
        )

    async def search_bm25_by_text(
        self,
        query: str,
        event_ids: List[str],
        k: int = 100,
        source_config_ids: Optional[List[str]] = None,
        fields: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Run a BM25 text search restricted to a candidate event_id set.

        Args:
            query: the query text
            event_ids: the candidate event ID list
            k: how many to return
            source_config_ids: source ID list
            fields: the BM25 query fields, for example ["title^2", "content"]

        Returns:
            [{"event_id": str, "_score": float}, ...]
        """
        if not query or not event_ids:
            return []

        fields = fields or ["title^2", "content"]
        filters: List[Dict[str, Any]] = [{"terms": {"event_id": event_ids}}]
        if source_config_ids:
            if len(source_config_ids) == 1:
                filters.append({"term": {"source_config_id": source_config_ids[0]}})
            else:
                filters.append({"terms": {"source_config_id": source_config_ids}})

        query_body = {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query,
                            "fields": fields,
                        }
                    }
                ],
                "filter": filters,
            }
        }
        routing = source_config_ids[0] if source_config_ids and len(source_config_ids) == 1 else None

        response = await self.es_client.search(
            index=self.INDEX_NAME,
            query=query_body,
            size=min(k, len(event_ids)),
            routing=routing,
            return_full_response=True,
            **{"_source": {"includes": ["event_id"]}},
        )

        results: List[Dict[str, Any]] = []
        for hit in response.get("hits", []):
            source = hit.get("source") or {}
            eid = source.get("event_id") or hit.get("id")
            if eid:
                results.append({"event_id": eid, "_score": hit.get("score", 0.0) or 0.0})
        return results

    async def search_similar_by_content(
        self,
        query_vector: List[float],
        k: int = 10,
        source_config_id: Optional[str] = None,
        source_config_ids: Optional[List[str]] = None,
        category: Optional[str] = None,
        event_ids: Optional[List[str]] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        source_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search similar events by the content vector

        Args:
            query_vector: the query vector
            k: how many to return
            source_config_id: source ID (single, backwards compatible)
            source_config_ids: source ID list (multi-source search)
            category: category (optional)
            start_time: start of the time range (optional)
            end_time: end of the time range (optional)
            source_ids: event source ID list (Article/Conversation ID, optional)

        Returns:
            The similar event list
        """
        return await self._vector_search(
            "content_vector",
            query_vector,
            k,
            source_config_id,
            source_config_ids,
            category,
            event_ids,
            start_time,
            end_time,
            source_ids,
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

        # Convert to a numpy array to check it
        try:
            np_array = np.array(vector, dtype=np.float32)
            # Check for NaN or Inf
            return not (np.isnan(np_array).any() or np.isinf(np_array).any())
        except (ValueError, TypeError):
            # A failed conversion means the vector holds invalid values
            return False

    async def _vector_search(
        self,
        vector_field: str,
        query_vector: List[float],
        k: int,
        source_config_id: Optional[str] = None,
        source_config_ids: Optional[List[str]] = None,
        category: Optional[str] = None,
        event_ids: Optional[List[str]] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        source_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Vector search (internal method)

        Args:
            vector_field: the vector field name
            query_vector: the query vector
            k: how many to return
            source_config_id: source ID (single, backwards compatible)
            source_config_ids: source ID list (multi-source search)
            category: category
            start_time: start of the time range (optional)
            end_time: end of the time range (optional)
            source_ids: event source ID list (optional)

        Returns:
            The similar event list
        """
        # Validate the query vector
        if not self._is_valid_vector(query_vector):
            raise ValueError("The query vector contains invalid values (NaN or Inf)")

        # Argument compatibility: source_config_ids wins, falling back to source_config_id
        if not source_config_ids and source_config_id:
            source_config_ids = [source_config_id]



        # Add the filter conditions
        must_filters: List[Dict[str, Any]] = []
        if source_config_ids:
            # A single source uses a term query, several sources use a terms query
            if len(source_config_ids) == 1:
                must_filters.append(
                    Q("term", source_config_id=source_config_ids[0]).to_dict())
            else:
                must_filters.append(
                    Q("terms", source_config_id=source_config_ids).to_dict())
        if category:
            must_filters.append(Q("term", category=category).to_dict())
        if event_ids:
            must_filters.append(Q("terms", event_id=event_ids).to_dict())
        if source_ids:
            must_filters.append(Q("terms", source_id=source_ids).to_dict())

        # Time filtering:
        # 1) with end_time: interval overlap
        # 2) without end_time: only start_time must fall inside the query range
        time_filter = self._build_time_range_filter(start_time, end_time)
        if time_filter:
            must_filters.append(time_filter)

        # Build the filter
        filter_query = None
        if must_filters:
            filter_query = {"bool": {"must": must_filters}}

        # source_config_id as the routing key improves query performance
        # Routing is only used for a single source; it is disabled for several, so the query can cross shards
        routing = source_config_ids[0] if source_config_ids and len(
            source_config_ids) == 1 else None

        # Use the vector_search method
        return await self.es_client.vector_search(
            index=self.INDEX_NAME,
            field=vector_field,
            vector=query_vector,
            size=k,
            filter_query=filter_query,
            routing=routing,
        )

    @staticmethod
    def _build_time_range_filter(
        start_time: Optional[datetime],
        end_time: Optional[datetime],
    ) -> Optional[Dict[str, Any]]:
        """
        Build the ES time range filter (interval overlap semantics).

        Note: the logic matches pipeline.modules.search.time_filter.build_es_time_range_filter;
        this is a local copy that avoids a circular import (event_repository -> modules.search -> event_repository).
        """
        if not start_time and not end_time:
            return None

        start_range: Dict[str, Any] = {}
        if start_time:
            start_range["gte"] = start_time
        if end_time:
            start_range["lte"] = end_time

        overlap_must: List[Dict[str, Any]] = [{"exists": {"field": "end_time"}}]
        if end_time:
            overlap_must.append({"range": {"start_time": {"lte": end_time}}})
        if start_time:
            overlap_must.append({"range": {"end_time": {"gte": start_time}}})

        overlap_clause = {"bool": {"must": overlap_must}}
        point_clause = {
            "bool": {
                "must": [
                    {"bool": {"must_not": {"exists": {"field": "end_time"}}}},
                    {"range": {"start_time": start_range}},
                ]
            }
        }

        return {
            "bool": {
                "should": [overlap_clause, point_clause],
                "minimum_should_match": 1,
            }
        }

    async def search_by_time_range(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        source_config_id: Optional[str] = None,
        size: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Search by time range (interval overlap: returns events intersecting the query period)

        An event with an end_time is judged by interval overlap;
        an event without one is judged by whether start_time falls inside the query range.

        Args:
            start_time: start time (optional)
            end_time: end time (optional)
            source_config_id: source ID (optional)
            size: how many to return

        Returns:
            The event list
        """
        s = Search(using=self.es_client, index=self.INDEX_NAME)

        # Time range filtering (matching the vector search rules)
        time_filter = self._build_time_range_filter(start_time, end_time)
        if time_filter:
            s = s.filter(Q(time_filter))

        if source_config_id:
            s = s.filter("term", source_config_id=source_config_id)

        s = s.sort("-created_time")  # newest first
        s = s[:size]

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
        return response

    async def search_by_entities(
        self, entity_ids: List[str], source_config_id: Optional[str] = None, size: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Search events by their related entities

        Args:
            entity_ids: entity ID list
            source_config_id: source ID (optional)
            size: how many to return

        Returns:
            The event list
        """
        s = Search(using=self.es_client, index=self.INDEX_NAME)

        # entity_ids is an array field, so a terms query is used
        s = s.filter("terms", entity_ids=entity_ids)

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
            routing=routing
        )
        return response
