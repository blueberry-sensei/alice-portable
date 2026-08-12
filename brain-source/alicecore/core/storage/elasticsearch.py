"""
Elasticsearch storage client

Supports vector search and full-text search
"""

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

from elasticsearch import AsyncElasticsearch
from elasticsearch.exceptions import NotFoundError

from alicecore.core.config import get_settings
from alicecore.exceptions import StorageError
from alicecore.utils import get_logger

logger = get_logger("storage.elasticsearch")


@dataclass
class ESConfig:
    """ES configuration class"""

    hosts: Union[str, List[str]]
    username: Optional[str] = None
    password: Optional[str] = None
    scheme: str = "http"
    timeout: int = 120
    max_connections: int = 30
    max_retries: int = 3
    verify_certs: bool = False

    @classmethod
    def from_env(cls) -> "ESConfig":
        """Build the configuration from environment variables"""
        # Compose the full address from ES_HOST and ES_PORT
        es_host = os.getenv("ES_HOST", "localhost")
        es_port = os.getenv("ES_PORT", "9201")
        hosts = f"{es_host}:{es_port}"

        # Handle a multi-host configuration (ES_HOSTS, comma separated)
        hosts_env = os.getenv("ES_HOSTS")
        if hosts_env:
            hosts = [host.strip() for host in hosts_env.split(",")]

        return cls(
            hosts=hosts,
            username=os.getenv("ES_USERNAME", "elastic"),
            password=os.getenv("ELASTIC_PASSWORD"),
            scheme=os.getenv("ES_SCHEME", "http"),
            timeout=int(os.getenv("ES_TIMEOUT", "300")),
            max_connections=int(os.getenv("ES_MAX_CONNECTIONS", "10")),
            max_retries=int(os.getenv("ES_MAX_RETRIES", "3")),
            verify_certs=os.getenv(
                "ES_VERIFY_CERTS", "false").lower() == "true",
        )


class ElasticsearchClient:
    """Asynchronous Elasticsearch client"""

    def __init__(
        self,
        hosts: Optional[List[str]] = None,
        config: Optional[ESConfig] = None,
        **kwargs: Any,
    ) -> None:
        """
        Initialise the Elasticsearch client

        Args:
            hosts: ES host list (optional, highest priority, backwards compatible)
            config: ES configuration object (optional, uses ESConfig)
            **kwargs: other parameters
        """
        settings = get_settings()

        # Priority: the hosts argument > the config object > the configuration file
        if hosts:
        # Backwards compatible: the hosts argument wins
            self.hosts = hosts
            client_config = {
                "hosts": self.hosts,
                **kwargs,
            }
        elif config:
        # Use the ESConfig configuration
            raw_hosts = config.hosts if isinstance(
                config.hosts, list) else [config.hosts]

        # Convert hosts into full URLs (scheme included)
            self.hosts = []
            for host in raw_hosts:
                if not host.startswith("http://") and not host.startswith("https://"):
                    # No scheme, add one
                    self.hosts.append(f"{config.scheme}://{host}")
                else:
                    self.hosts.append(host)

            # Build the client configuration
            client_config = {
                **kwargs,
            }

            # When credentials are given, embed them in the URL
            if config.username and config.password:
                hosts_with_auth = []
                for host in self.hosts:
                    # Parse the URL and add the credentials
                    from urllib.parse import urlparse
                    parsed = urlparse(host)
                    auth_url = f"{parsed.scheme}://{config.username}:{config.password}@{parsed.netloc}{parsed.path}"
                    hosts_with_auth.append(auth_url)
                client_config["hosts"] = hosts_with_auth
            else:
                client_config["hosts"] = self.hosts

            # Add the remaining configuration
            client_config["request_timeout"] = config.timeout
            client_config["max_retries"] = config.max_retries
            client_config["verify_certs"] = config.verify_certs
        else:
            # Use es_url from the configuration file
            self.hosts = settings.es_url

            # Default to the ES_TIMEOUT environment variable, or 300s
            default_timeout = int(os.getenv("ES_TIMEOUT", "300"))

            # Build the full URL with credentials, or use basic_auth
            if settings.es_username and settings.es_password:
                # Parse the URL and add the credentials
                from urllib.parse import urlparse
                parsed = urlparse(settings.es_url)
                # Rebuild the URL with credentials
                auth_url = f"{parsed.scheme}://{settings.es_username}:{settings.es_password}@{parsed.netloc}{parsed.path}"
                client_config = {
                    "hosts": [auth_url],
                    "request_timeout": default_timeout,  # add the default timeout
                    **kwargs,
                }
            else:
                client_config = {
                    "hosts": [settings.es_url],
                    "request_timeout": default_timeout,  # add the default timeout
                    **kwargs,
                }

        # Create the client
        self.client = AsyncElasticsearch(**client_config)
        self._ensured_indices: set = set()  # indices whose explicit mapping has been created

        logger.info("Elasticsearch client initialised", extra={"hosts": self.hosts})

    async def _ensure_index_mapping(self, index: str, documents: List[Dict[str, Any]]) -> None:
        """Before the first write to an index, create the explicit mapping from index_schemas (only when it is missing).

        The point: relying on the ES dynamic mapping would map ``source_config_id`` and friends to ``text`` (analysed), so a
        ``term`` filter never matches a UUID and search returns nothing. The explicit mapping makes them ``keyword`` and vectors
        ``dense_vector`` (dimensions inferred from the first vector), so kNN + filtering works out of the box.
        """
        if index in self._ensured_indices:
            return
        try:
            if await self.index_exists(index):
                self._ensured_indices.add(index)
                return
            from alicecore.core.storage.index_schemas import INDEX_SCHEMAS

            decl = INDEX_SCHEMAS.get(index)
            dims = None
            for doc in documents:
                for f in decl.vector_fields if decl else ():
                    v = doc.get(f)
                    if isinstance(v, (list, tuple)) and len(v) > 8:
                        dims = len(v)
                        break
                if dims:
                    break
            if dims is None:
                dims = getattr(get_settings(), "embedding_dimensions", None) or 1024

            props: Dict[str, Any] = {"id": {"type": "keyword"}}
            if decl is not None:
                for f in decl.keyword_fields + decl.array_fields:
                    props[f] = {"type": "keyword"}
                for f in decl.text_fields:
                    props[f] = {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
                    }
                for f in decl.bool_fields:
                    props[f] = {"type": "boolean"}
                for f in decl.datetime_fields:
                    props[f] = {"type": "date"}
                for f in decl.int_fields:
                    props[f] = {"type": "integer"}
                for f in decl.vector_fields:
                    props[f] = {
                        "type": "dense_vector",
                        "dims": dims,
                        "index": True,
                        "similarity": "cosine",
                    }
            await self.create_index(index, mappings={"properties": props})
        except Exception as e:
            logger.warning(f"Failed to ensure the ES index mapping (index={index}), falling back to the dynamic mapping: {e}")
        finally:
            self._ensured_indices.add(index)

    def __getattr__(self, name: str):
        """
        Delegate undefined methods to the underlying AsyncElasticsearch client

        That lets ElasticsearchClient offer both:
        - the custom methods (vector_search, index_document, get_document and so on)
        - the native AsyncElasticsearch methods (search, index, get, delete and so on)

        so a repository can use one ElasticsearchClient object and call either the custom methods
        or the native ones.
        """
        return getattr(self.client, name)

    async def index_document(
        self,
        index: str,
        document: Dict[str, Any],
        doc_id: Optional[str] = None,
        routing: Optional[str] = None,
    ) -> str:
        """
        Index a document

        Args:
            index: index name
            document: document content
            doc_id: document ID (optional)
            routing: routing key (optional, picks the shard)

        Returns:
            The document ID

        Raises:
            StorageError: indexing failed
        """
        try:
            await self._ensure_index_mapping(index, [document])
            response = await self.client.index(
                index=index,
                document=document,
                id=doc_id,
                routing=routing,
            )
            return response["_id"]
        except Exception as e:
            logger.error(f"Indexing the document failed: {e}", exc_info=True)
            raise StorageError(f"Indexing the document failed: {e}") from e

    async def get_document(
        self,
        index: str,
        doc_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Get a document

        Args:
            index: index name
            doc_id: document ID

        Returns:
            The document content, or None when it does not exist
        """
        try:
            response = await self.client.get(index=index, id=doc_id)
            return response["_source"]
        except NotFoundError:
            return None
        except Exception as e:
            logger.error(f"Getting the document failed: {e}", exc_info=True)
            raise StorageError(f"Getting the document failed: {e}") from e

    async def delete_document(
        self,
        index: str,
        doc_id: str,
    ) -> bool:
        """
        Delete a document

        Args:
            index: index name
            doc_id: document ID

        Returns:
            True when the delete succeeded
        """
        try:
            await self.client.delete(index=index, id=doc_id)
            return True
        except NotFoundError:
            return False
        except Exception as e:
            logger.error(f"Deleting the document failed: {e}", exc_info=True)
            raise StorageError(f"Deleting the document failed: {e}") from e

    async def update_document(
        self,
        index: str,
        doc_id: str,
        update_data: Dict[str, Any],
    ) -> bool:
        """
        Partially update a document

        Args:
            index: index name
            doc_id: document ID
            update_data: the data to update

        Returns:
            True when the update succeeded

        Raises:
            StorageError: the update failed
        """
        try:
            await self.client.update(
                index=index,
                id=doc_id,
                doc=update_data,
            )
            logger.info(f"Document {doc_id} updated")
            return True
        except NotFoundError:
            logger.warning(f"Document {doc_id} does not exist")
            return False
        except Exception as e:
            logger.error(f"Updating the document failed: {e}", exc_info=True)
            raise StorageError(f"Updating the document failed: {e}") from e

    async def count_documents(
        self,
        index: str,
        query: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Count the documents

        Args:
            index: index name
            query: the query, matching every document by default

        Returns:
            The document count

        Raises:
            StorageError: counting failed
        """
        try:
            if query is None:
                query = {"match_all": {}}

            response = await self.client.count(index=index, query=query)
            return response["count"]
        except Exception as e:
            logger.error(f"Counting the documents failed: {e}", exc_info=True)
            raise StorageError(f"Counting the documents failed: {e}") from e

    async def get_mapping(self, index: str) -> Dict[str, Any]:
        """
        Get the index mapping

        Args:
            index: index name

        Returns:
            The index mapping

        Raises:
            StorageError: the read failed
        """
        try:
            response = await self.client.indices.get_mapping(index=index)
            return response.get(index, {}).get("mappings", {})
        except Exception as e:
            logger.error(f"Getting the index mapping failed: {e}", exc_info=True)
            raise StorageError(f"Getting the index mapping failed: {e}") from e

    async def search(
        self,
        index: str,
        query: Dict[str, Any],
        size: int = 10,
        from_: int = 0,
        return_full_response: bool = False,
        routing: Optional[str] = None,
        **kwargs: Any,
    ) -> Union[List[Dict[str, Any]], Dict[str, Any]]:
        """
        Search documents

        Args:
            index: index name
            query: the query DSL
            size: how many to return
            from_: the offset
            return_full_response: whether to return the full response (total, max_score and so on)
            routing: routing key (optional, picks the shard)
            **kwargs: other parameters

        Returns:
            return_full_response=False: the document list (backwards compatible)
            return_full_response=True: the full response dictionary {total, max_score, hits}
        """
        try:
            search_params = {
                "index": index,
                "query": query,
                "size": size,
                "from_": from_,
                "timeout": "300s",  # a query-level timeout, so heavy concurrency cannot wait forever
                **kwargs,
            }
            if routing:
                search_params["routing"] = routing

            response = await self.client.search(**search_params)

            if return_full_response:
                # Return the full information
                hits = response.get("hits", {})
                return {
                    "total": hits.get("total", {}).get("value", 0),
                    "max_score": hits.get("max_score", 0),
                    "hits": [
                        {
                            "id": hit.get("_id"),
                            "score": hit.get("_score"),
                            "source": hit.get("_source"),
                            "index": hit.get("_index"),
                        }
                        for hit in hits.get("hits", [])
                    ],
                }
            else:
                # Backwards compatible: return only the document list
                return [hit["_source"] for hit in response["hits"]["hits"]]
        except NotFoundError:
            # The index does not exist yet -> treat it as no result rather than crashing (returning the shape the caller expects)
            logger.warning(f"Search: index '{index}' does not exist, returning an empty result")
            if return_full_response:
                return {"total": 0, "max_score": 0, "hits": []}
            return []
        except Exception as e:
            logger.error(f"Searching the documents failed: {e}", exc_info=True)
            raise StorageError(f"Searching the documents failed: {e}") from e

    async def vector_search(
        self,
        index: str,
        field: str,
        vector: List[float],
        size: int = 10,
        filter_query: Optional[Dict[str, Any]] = None,
        routing: Optional[str] = None,
        include_vector: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Vector search

        Args:
            index: index name
            field: the vector field name
            vector: the query vector
            size: how many to return
            filter_query: the filter
            routing: routing key (optional, picks the shard)
            include_vector: whether the vector field is returned (default False, saving roughly 4KB per row)

        Returns:
            The similar document list (with a _score field)
        """
        try:
            knn_query: Dict[str, Any] = {
                "field": field,
                "query_vector": vector,
                "k": size,
                "num_candidates": max(100, size * 2),  # a fixed candidate count; k never exceeds 100
            }

            if filter_query:
                knn_query["filter"] = filter_query

            search_params = {
                "index": index,
                "knn": knn_query,
                "size": size,
                "timeout": "300s",  # a query-level timeout, so it cannot wait forever
            }

            # The parameter decides whether the vector field is excluded
            if not include_vector:
                search_params["_source"] = {"excludes": ["vector"]}

            if routing:
                search_params["routing"] = routing

            response = await self.client.search(**search_params)

            return [
                {**hit["_source"], "_score": hit["_score"]}
                for hit in response["hits"]["hits"]
            ]
        except NotFoundError:
            # The index does not exist yet (no entity/event vector written) -> treat it as no result rather than crashing
            logger.warning(f"Vector search: index '{index}' does not exist, returning an empty result")
            return []
        except Exception as e:
            logger.error(f"Vector search failed: {e}", exc_info=True)
            raise StorageError(f"Vector search failed: {e}") from e

    async def bulk_index(
        self,
        index: str,
        documents: List[Dict[str, Any]],
        return_details: bool = False,
        routing: Optional[str] = None,
    ) -> Union[int, Dict[str, Any]]:
        """
        Index documents in batch

        Args:
            index: index name
            documents: the document list
            return_details: whether the details (the error list) are returned
            routing: routing key (optional, picks the shard)

        Returns:
            return_details=False: how many documents were indexed (backwards compatible)
            return_details=True: the detailed result dictionary {success, total, success_count, error_count, errors}
        """
        from elasticsearch.helpers import async_bulk

        try:
            if not documents:
                if return_details:
                    return {
                        "success": True,
                        "total": 0,
                        "success_count": 0,
                        "error_count": 0,
                        "errors": [],
                    }
                return 0

            await self._ensure_index_mapping(index, documents)

            actions = [
                {
                    "_index": index,
                    "_source": doc,
                    "_id": doc.get("id"),
                    **({"_routing": routing} if routing else {}),
                }
                for doc in documents
            ]

            success_count, errors = await async_bulk(
                self.client, actions, raise_on_error=False, stats_only=False
            )

            error_count = len(errors) if isinstance(errors, list) else 0
            logger.info(f"Batch indexing finished: {success_count} succeeded, {error_count} failed")

            if return_details:
                # Return the details
                error_list = []
                if isinstance(errors, list):
                    for error in errors:
                        if isinstance(error, dict):
                            error_list.append(
                                {
                                    "id": error.get("index", {}).get("_id"),
                                    "error": error.get("index", {}).get(
                                        "error", "Unknown error"
                                    ),
                                }
                            )

                return {
                    "success": error_count == 0,
                    "total": len(documents),
                    "success_count": success_count,
                    "error_count": error_count,
                    "errors": error_list,
                }
            else:
                # Backwards compatible: return only the success count
                return success_count
        except Exception as e:
            logger.error(f"Batch indexing failed: {e}", exc_info=True)
            if return_details:
                return {
                    "success": False,
                    "total": len(documents),
                    "success_count": 0,
                    "error_count": len(documents),
                    "errors": [{"error": str(e)}],
                }
            raise StorageError(f"Batch indexing failed: {e}") from e

    async def create_index(
        self,
        index: str,
        mappings: Dict[str, Any],
        settings: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Create an index

        Args:
            index: index name
            mappings: the mapping definition
            settings: the index settings

        Returns:
            True when the index was created
        """
        try:
            # Elasticsearch 8.x no longer takes a body parameter
            await self.client.indices.create(
                index=index,
                mappings=mappings,
                settings=settings or {}
            )
            logger.info(f"Index created: {index}")
            return True
        except Exception as e:
            logger.error(f"Creating the index failed: {e}", exc_info=True)
            raise StorageError(f"Creating the index failed: {e}") from e

    async def delete_index(self, index: str) -> bool:
        """
        Delete an index

        Args:
            index: index name

        Returns:
            True when the index was deleted
        """
        try:
            await self.client.indices.delete(index=index)
            logger.info(f"Index deleted: {index}")
            return True
        except NotFoundError:
            return False
        except Exception as e:
            logger.error(f"Deleting the index failed: {e}", exc_info=True)
            raise StorageError(f"Deleting the index failed: {e}") from e

    async def index_exists(self, index: str) -> bool:
        """
        Check whether an index exists

        Args:
            index: index name

        Returns:
            True when it exists
        """
        return await self.client.indices.exists(index=index)

    async def close(self) -> None:
        """Close the client connection"""
        await self.client.close()
        logger.info("Elasticsearch connection closed")

    async def ping(self) -> bool:
        """
        Test the connection

        Returns:
            True when the connection succeeded
        """
        try:
            return await self.client.ping()
        except Exception as e:
            logger.error(f"The ES connection test failed: {e}")
            return False

    async def check_connection(self) -> bool:
        """
        Check the ES connection and read the version

        Returns:
            Whether the connection is healthy
        """
        try:
            info = await self.client.info()
            version = info.get("version", {}).get("number", "unknown")
            logger.info(f"ES connection is healthy, version: {version}")
            return True
        except Exception as e:
            logger.error(f"The ES connection check failed: {e}")
            return False


# The vector backend singleton moved to the provider-neutral ``core.storage.client`` (so the default path does not import elasticsearch).
# The name is re-exported here for backwards compatibility with the historical ``from ...storage.elasticsearch import get_es_client`` call.
from alicecore.core.storage.client import (  # noqa: E402
    close_es_client,
    get_es_client,
    reset_es_client,
)

__all__ = [
    "ESConfig",
    "ElasticsearchClient",
    "get_es_client",
    "reset_es_client",
    "close_es_client",
]
