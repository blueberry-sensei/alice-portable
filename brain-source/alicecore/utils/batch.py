"""
Batch processing utilities

Provides the batch logic for vector generation and ES indexing
"""

import time
from typing import Any, Callable, Dict, List, Optional, TypeVar

from alicecore.utils import get_logger, is_retryable_error

logger = get_logger("utils.batch")

T = TypeVar('T')


class BatchProcessor:
    """Base class for batch processors"""

    def __init__(
        self,
        batch_size: int = 10,
        logger_name: Optional[str] = None,
    ):
        """
        Initialise the batch processor

        Args:
            batch_size: batch size
            logger_name: logger name
        """
        self.batch_size = batch_size
        self.logger = get_logger(logger_name or "utils.batch")


class EmbeddingBatchProcessor(BatchProcessor):
    """Batch processor for vector generation"""

    async def process(
        self,
        items: List[T],
        text_extractor: Callable[[T], str],
        embedding_client,
        on_success: Optional[Callable[[T, List[float]], Any]] = None,
    ) -> Dict[str, Any]:
        """
        Generate vectors in batch

        Args:
            items: items to process
            text_extractor: text extraction function (item -> text)
            embedding_client: vector client
            on_success: success callback (item, vector) -> result

        Returns:
            A statistics dictionary {total, success, failed, results}
        """
        start_time = time.perf_counter()
        results = []
        failed_count = 0

        for i in range(0, len(items), self.batch_size):
            batch = items[i : i + self.batch_size]

            try:
                texts = [text_extractor(item) for item in batch]
                vectors = await embedding_client.batch_generate(texts)

                # Verify the returned count
                if len(vectors) != len(batch):
                    raise ValueError(
                        f"batch_generate returned mismatched vector count: "
                        f"expected={len(batch)}, actual={len(vectors)}"
                    )

                # Handle the successful items
                for item, vector in zip(batch, vectors):
                    if on_success:
                        result = on_success(item, vector)
                        results.append(result)

            except Exception as e:
                self.logger.warning(f"Batch vector generation failed, degrading to a retry: {e}")
                # Degrade: retry one by one
                for item in batch:
                    try:
                        text = text_extractor(item)
                        vector = await embedding_client.generate(text)
                        if on_success:
                            result = on_success(item, vector)
                            results.append(result)
                    except Exception as retry_e:
                        self.logger.error(f"Single vector generation failed: {retry_e}")
                        failed_count += 1
                        # Record whether it is retryable
                        if is_retryable_error(retry_e):
                            self.logger.warning(f"Vector generation failed (retryable)")
                        else:
                            self.logger.error(f"Vector generation failed (not retryable)")

        total_time = time.perf_counter() - start_time

        return {
            "total": len(items),
            "success": len(results),
            "failed": failed_count,
            "results": results,
            "time": f"{total_time:.2f}s",
        }


class ESBulkIndexProcessor(BatchProcessor):
    """Batch processor for ES indexing"""

    async def process(
        self,
        documents: List[Dict[str, Any]],
        es_client,
        index_name: str,
        routing: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Index into ES in batch

        Args:
            documents: document list
            es_client: ES client
            index_name: index name
            routing: routing value

        Returns:
            A statistics dictionary {total, indexed, failed}
        """
        start_time = time.perf_counter()
        indexed = 0
        failed = 0

        for i in range(0, len(documents), self.batch_size):
            batch = documents[i : i + self.batch_size]

            try:
                result = await es_client.bulk_index(
                    index=index_name,
                    documents=batch,
                    return_details=True,
                    routing=routing,
                )

                indexed += result["success_count"]

                if result["error_count"] > 0:
                    failed_ids = {err["id"] for err in result["errors"]}
                    for doc in batch:
                        if doc["id"] in failed_ids:
                            try:
                                await es_client.index_document(
                                    index=index_name,
                                    document=doc,
                                    doc_id=doc["id"],
                                    routing=routing,
                                )
                                indexed += 1
                            except Exception as retry_e:
                                self.logger.error(f"Retry indexing failed: {doc['id']}: {retry_e}")
                                failed += 1

            except Exception as e:
                self.logger.error(f"Batch indexing failed, degrading to a retry: {e}")
                # Degrade: retry the whole batch one by one
                for doc in batch:
                    try:
                        await es_client.index_document(
                            index=index_name,
                            document=doc,
                            doc_id=doc["id"],
                            routing=routing,
                        )
                        indexed += 1
                    except Exception as retry_e:
                        self.logger.error(f"Degraded indexing failed: {doc['id']}: {retry_e}")
                        failed += 1
                        # Record whether it is retryable
                        if is_retryable_error(retry_e):
                            self.logger.warning(f"ES indexing failed (retryable): {doc['id']}")
                        else:
                            self.logger.error(f"ES indexing failed (not retryable): {doc['id']}")

        total_time = time.perf_counter() - start_time

        return {
            "total": len(documents),
            "indexed": indexed,
            "failed": failed,
            "time": f"{total_time:.2f}s",
        }


async def batch_generate_embeddings(
    items: List[T],
    text_extractor: Callable[[T], str],
    embedding_client,
    batch_size: int = 10,
    on_success: Optional[Callable[[T, List[float]], Any]] = None,
) -> Dict[str, Any]:
    """
    Generate vectors in batch (convenience function)

    Args:
        items: items to process
        text_extractor: text extraction function (item -> text)
        embedding_client: vector client
        batch_size: batch size
        on_success: success callback (item, vector) -> result

    Returns:
        A statistics dictionary
    """
    processor = EmbeddingBatchProcessor(batch_size=batch_size)
    return await processor.process(items, text_extractor, embedding_client, on_success)


async def batch_index_to_es(
    documents: List[Dict[str, Any]],
    es_client,
    index_name: str,
    batch_size: int = 50,
    routing: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Index into ES in batch (convenience function)

    Args:
        documents: document list
        es_client: ES client
        index_name: index name
        batch_size: batch size
        routing: routing value

    Returns:
        A statistics dictionary
    """
    processor = ESBulkIndexProcessor(batch_size=batch_size)
    return await processor.process(documents, es_client, index_name, routing)
