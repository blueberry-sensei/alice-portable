"""
Source chunk repository

Provides the business query methods for source chunks (SourceChunk)
"""

from typing import Any, Dict, List, Optional

from alicecore.core.storage.query import Q, Search

from alicecore.core.storage.repositories.base import BaseRepository


class SourceChunkRepository(BaseRepository):
    """Source chunk repository"""

    INDEX_NAME = "source_chunks"

    async def index_chunk(
        self,
        chunk_id: str,
        source_id: str,
        source_config_id: str,
        rank: int,
        heading: Optional[str],
        content: str,
        heading_vector: Optional[List[float]],
        content_vector: List[float],
        references: Optional[List[str]] = None,
        **kwargs,
    ) -> str:
        """
        Index one source chunk

        Args:
            chunk_id: chunk ID (SourceChunk.id)
            source_id: source ID (Article.id or Conversation.id)
            source_config_id: source configuration ID
            rank: sort order
            heading: heading
            content: content
            heading_vector: the heading vector
            content_vector: the content vector
            references: the related ArticleSection ID list
            **kwargs: other fields (chunk_type, content_length and so on)

        Returns:
            The document ID
        """
        document = {
            "chunk_id": chunk_id,
            "source_id": source_id,
            "source_config_id": source_config_id,
            "rank": rank,
            "heading": heading,
            "content": content,
            "heading_vector": heading_vector,
            "content_vector": content_vector,
            "references": references or [],
            **kwargs,
        }

        # source_config_id is the routing key, so one source's data lands on one shard
        return await self.index_document(
            self.INDEX_NAME, chunk_id, document, routing=source_config_id
        )

    async def get_by_source(
        self, source_id: str, sort_by_rank: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Get every chunk of a source

        Args:
            source_id: source ID (Article.id or Conversation.id)
            sort_by_rank: whether to sort by rank

        Returns:
            The chunk list
        """
        s = Search(using=self.es_client, index=self.INDEX_NAME)

        s = s.filter("term", source_id=source_id)

        if sort_by_rank:
            s = s.sort("rank")

        s = s[:100]  # at most 100 chunks are returned

        # Convert to a dictionary and execute
        search_dict = s.to_dict()
        response = await self.es_client.search(
            index=self.INDEX_NAME,
            query=search_dict.get("query", {}),
            size=search_dict.get("size", 10),
            return_full_response=True,
        )
        return [hit["source"] for hit in response.get("hits", [])]

    async def search_similar_by_content(
        self,
        query_vector: List[float],
        k: int = 10,
        source_config_id: Optional[str] = None,
        source_config_ids: Optional[List[str]] = None,
        chunk_type: Optional[str] = None,
        chunk_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search similar chunks by the content vector

        Args:
            query_vector: the query vector
            k: how many to return
            source_config_id: source ID (single, backwards compatible)
            source_config_ids: source ID list (multi-source search)
            chunk_type: chunk type (optional)
            chunk_ids: chunk ID list (optional, restricts the search to those chunks)

        Returns:
            The similar chunk list
        """


        # Add the filter conditions
        filter_query = None
        filters = []

        # Source filtering (one or several)
        if source_config_ids:
            # source_config_ids (plural) wins
            filters.append(Q("terms", source_config_id=source_config_ids))
        elif source_config_id:
            # Backwards compatible with source_config_id (singular)
            filters.append(Q("term", source_config_id=source_config_id))

        if chunk_type:
            filters.append(Q("term", chunk_type=chunk_type))

        # chunk_ids filter (restricts the search to the given chunks)
        if chunk_ids:
            filters.append(Q("terms", _id=chunk_ids))

        if filters:
            filter_query = Q("bool", must=filters).to_dict()

        # source_config_id as the routing key improves query performance (single source only)
        routing = source_config_id if source_config_id else None

        # Use the vector_search method
        return await self.es_client.vector_search(
            index=self.INDEX_NAME,
            field="content_vector",
            vector=query_vector,
            size=k,
            filter_query=filter_query,
            routing=routing,
        )

    async def search_by_text(
        self,
        query: str,
        source_config_id: Optional[str] = None,
        source_config_ids: Optional[List[str]] = None,
        size: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Full-text search over the chunks

        Args:
            query: the query text
            source_config_id: source ID (single, backwards compatible)
            source_config_ids: source ID list (multi-source search)
            size: how many to return

        Returns:
            The chunk list
        """
        s = Search(using=self.es_client, index=self.INDEX_NAME)

        # Multi-field query
        s = s.query(
            "multi_match",
            query=query,
            fields=["heading^2", "content"],  # heading carries the higher weight
        )

        # Source filtering (one or several)
        if source_config_ids:
            # source_config_ids (plural) wins
            s = s.filter("terms", source_config_id=source_config_ids)
        elif source_config_id:
            # Backwards compatible with source_config_id (singular)
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

    async def get_chunks_by_ids(
        self,
        chunk_ids: List[str],
        include_vectors: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Get the detailed chunk information (vectors included) in batch by chunk ID

        Args:
            chunk_ids: chunk ID list
            include_vectors: whether the vector fields are included (default True)

        Returns:
            The detailed chunk list (content_vector and heading_vector included)
        """
        if not chunk_ids:
            return []

        # Build the ES query
        query_body = {
            "query": {
                "terms": {
                    "chunk_id": chunk_ids
                }
            },
            "size": len(chunk_ids)
        }

        # Exclude the vector fields when they are not needed
        if not include_vectors:
            query_body["_source"] = {
                "excludes": ["heading_vector", "content_vector"]
            }

        try:
            response = await self.es_client.search(
                index=self.INDEX_NAME,
                query=query_body["query"],
                size=query_body.get("size", 10),
                _source=query_body.get("_source")
            )

            results = []

            if isinstance(response, list):
                # Shape 2: a plain document list
                for chunk_data in response:
                    if isinstance(chunk_data, dict) and "chunk_id" in chunk_data:
                        results.append(chunk_data)

            elif isinstance(response, dict) and "hits" in response:
                # Shape 1: the full response
                hits = response["hits"].get("hits", [])
                for hit in hits:
                    if isinstance(hit, dict):
                        if "_source" in hit:
                            result = hit["_source"]
                            if "chunk_id" not in result:
                                result["chunk_id"] = hit.get("_id")
                            results.append(result)

            return results

        except Exception as e:
            import logging
            import traceback
            logger = logging.getLogger(__name__)
            logger.error(f"Batch reading the source chunks failed: {e}")
            logger.error(f"Details: {traceback.format_exc()}")
            return []
