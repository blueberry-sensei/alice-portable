"""
Elasticsearch repository base class
"""

from abc import ABC
from typing import Any, Dict, List, Optional


class BaseRepository(ABC):
    """Repository base class"""

    def __init__(self, es_client: Any):
        """
        Initialise the repository

        Args:
            es_client: the vector backend client (method surface matching ElasticsearchClient)
        """
        self.es_client = es_client

    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any], routing: Optional[str] = None
    ) -> str:
        """
        Index one document

        Args:
            index: index name
            doc_id: document ID
            document: document content
            routing: routing key (optional)

        Returns:
            The document ID
        """
        response = await self.es_client.index(
            index=index, id=doc_id, document=document, routing=routing
        )
        return response["_id"]

    async def get_document(self, index: str, doc_id: str) -> Optional[Dict[str, Any]]:
        """
        Get one document

        Args:
            index: index name
            doc_id: document ID

        Returns:
            The document content, or None when it does not exist
        """
        try:
            response = await self.es_client.get(index=index, id=doc_id)
            return response["_source"]
        except Exception:
            return None

    async def delete_document(self, index: str, doc_id: str) -> bool:
        """
        Delete one document

        Args:
            index: index name
            doc_id: document ID

        Returns:
            Whether the delete succeeded
        """
        try:
            await self.es_client.delete(index=index, id=doc_id)
            return True
        except Exception:
            return False

    async def bulk_index(
        self, index: str, documents: List[Dict[str, Any]], routing: Optional[str] = None
    ) -> Dict[str, int]:
        """
        Index documents in batch

        Args:
            index: index name
            documents: document list, each holding an _id field
            routing: routing key (optional)

        Returns:
            The statistics: {"success": 10, "failed": 0}
        """
        # Delegated to the vector backend client's bulk_index (a shared method surface across ES/LanceDB/pgvector/oceanbase),
        # so elasticsearch.helpers.async_bulk is no longer a direct dependency.
        detail = await self.es_client.bulk_index(
            index, documents, return_details=True, routing=routing
        )
        return {
            "success": detail.get("success_count", 0),
            "failed": detail.get("error_count", 0),
        }
