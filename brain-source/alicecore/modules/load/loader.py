"""
Document loader

Loads a document, drives the parser and processor, and saves into the database
"""

from abc import ABC, abstractmethod
import asyncio
import inspect
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import OperationalError

from alicecore.db import (
    Article,
    ArticleParseStatus,
    ArticleSection,
    SourceEvent,
    SourceChunk,
    SourceConfig,
    get_session_factory,
)
from alicecore.exceptions import AIError, LoadError, StorageError
from alicecore.modules.load.config import DocumentLoadConfig, LoadResult
from alicecore.modules.load.chunking import ChunkingResult
from alicecore.modules.load.parser import MarkdownParser
from alicecore.modules.load.processor import DocumentProcessor
from alicecore.utils import estimate_tokens, get_logger, normalize_heading_text, is_retryable_error
import uuid

logger = get_logger("modules.load.loader")


class BaseLoader(ABC):
    """Base loader"""

    def __init__(
        self,
        processor: Optional[DocumentProcessor] = None,
        progress_callback: Optional[Callable[[str], Optional[Awaitable[None]]]] = None,
    ) -> None:
        """
        Initialise the loader

        Args:
            processor: the document processor (a default is used when omitted)
        """
        self.processor = processor or DocumentProcessor()
        self._progress_callback = progress_callback
        self.session_factory = get_session_factory()
        logger.info(f"{self.__class__.__name__} initialised")

    @abstractmethod
    async def load(self, config) -> LoadResult:
        """
        Load the data (the main entry point)

        Args:
            config: the load configuration object

        Returns:
            LoadResult (holding source_id and chunk_ids)
        """
        pass

    @abstractmethod
    async def _save_to_database(self, *args, **kwargs) -> tuple[str, List[str]]:
        """
        Save into the database

        Returns:
            (source_id, chunk_ids)
        """
        pass

    async def _generate_embedding(self, text: str):
        """
        Generate the vector (delegated to the processor)

        Args:
            text: the text content

        Returns:
            The vector array
        """
        return await self.processor.generate_embedding(text)

    async def _notify_progress(self, message: str):
        """Report the progress of the current stage to the caller"""
        if not self._progress_callback:
            return
        try:
            result = self._progress_callback(message)
            if inspect.isawaitable(result):
                await result
        except Exception as e:  # noqa: BLE001
            logger.warning(f"The progress notification failed: {e}")

    async def _index_source_chunks_to_es(
        self, source_id: str, source_type: str
    ) -> None:
        """
        Index the SourceChunk rows into Elasticsearch (shared method)

        Args:
            source_id: source ID (UUID)
            source_type: source type ("ARTICLE" or "CHAT")
        """
        from alicecore.core.storage import SourceChunkRepository
        from alicecore.core.storage.client import get_es_client

        # The vector backend (ES or pgvector, per VECTOR_PROVIDER)
        es_client_wrapper = get_es_client()
        repo = SourceChunkRepository(es_client_wrapper.client)

        try:
            async with self.session_factory() as session:
                # Read every SourceChunk
                stmt = (
                    select(SourceChunk)
                    .where(
                        SourceChunk.source_id == source_id,
                        SourceChunk.source_type == source_type,
                    )
                    .order_by(SourceChunk.rank)
                )
                result = await session.execute(stmt)
                chunks = result.scalars().all()

                if not chunks:
                    logger.warning(
                        f"The source has no SourceChunk: {source_id} (type={source_type})"
                    )
                    return

                # Batch processing configuration
                embedding_batch_size = getattr(self, '_embedding_batch_size', 10)
                es_bulk_size = getattr(self, '_es_bulk_index_size', 50)

                stats = await self._batch_index_chunks(
                    chunks=chunks,
                    repo=repo,
                    es_client=es_client_wrapper,
                    embedding_batch_size=embedding_batch_size,
                    es_bulk_size=es_bulk_size,
                    source_config_id=chunks[0].source_config_id
                )
                logger.info(
                    f"SourceChunk batch indexing finished: {source_id} (type={source_type})",
                    extra=stats
                )

        except Exception as e:
            # KHÔNG nuốt. Chunk không có vector thì search không bao giờ tìm ra nó, mà tầng trên
            # lại thấy "load xong" và đánh document là READY — tri thức nằm trong DB, biến mất
            # khỏi recall, không ai biết. Đó là kiểu hỏng nguy hiểm nhất của sản phẩm này.
            # Ném lên để document đi vào FAILED kèm lý do đọc được.
            logger.error(f"Indexing failed: {source_id}: {e}", exc_info=True)
            raise
        finally:
            # Make sure the ES client is closed
            try:
                await es_client_wrapper.client.close()
            except Exception as close_err:
                logger.warning(f"Failed to close the ES client: {close_err}")

    async def _batch_index_chunks(
        self,
        chunks: List,
        repo,
        es_client,
        embedding_batch_size: int,
        es_bulk_size: int,
        source_config_id: str
    ) -> Dict[str, Any]:
        """
        Generate vectors and index into ES for the chunks in batch

        Args:
            chunks: the SourceChunk list
            repo: the SourceChunkRepository instance
            es_client: the ElasticsearchClient wrapper instance
            embedding_batch_size: vector generation batch size
            es_bulk_size: ES index batch size
            source_config_id: source configuration ID (used for routing)

        Returns:
            The statistics dictionary
        """
        import time
        from alicecore.core.ai.factory import get_embedding_client

        start_time = time.perf_counter()
        embedding_client = await get_embedding_client(scenario='general')

        documents = []
        embedding_failed = 0
        failed_chunk_ids: List[str] = []

        # === Stage 1: generate the vectors in batch ===
        for i in range(0, len(chunks), embedding_batch_size):
            batch_chunks = chunks[i:i+embedding_batch_size]

            try:
                # Prepare the text
                heading_texts = [c.heading for c in batch_chunks if c.heading]
                content_texts = [
                    f"{c.heading}\n\n{c.content[:1024]}"
                    for c in batch_chunks
                ]

                # Generate the vectors in batch
                heading_vectors = []
                if heading_texts:
                    heading_vectors = await embedding_client.batch_generate(heading_texts)

                content_vectors = await embedding_client.batch_generate(content_texts)

                # Build the document list
                heading_idx = 0
                for j, chunk in enumerate(batch_chunks):
                    heading_vec = None
                    if chunk.heading and heading_idx < len(heading_vectors):
                        heading_vec = heading_vectors[heading_idx]
                        heading_idx += 1

                    doc = {
                        "id": chunk.id,
                        "chunk_id": chunk.id,
                        "source_id": chunk.source_id,
                        "source_config_id": chunk.source_config_id,
                        "rank": chunk.rank,
                        "heading": chunk.heading,
                        "content": chunk.content,
                        "heading_vector": heading_vec,
                        "content_vector": content_vectors[j],
                        "references": chunk.references,
                        "chunk_type": "TEXT",
                        "content_length": chunk.chunk_length,
                    }
                    documents.append(doc)

            except Exception as e:
                logger.warning(f"Batch vector generation failed, degrading to a retry: {e}")
                # Degrade: retry one by one
                for chunk in batch_chunks:
                    try:
                        heading_vec = None
                        if chunk.heading:
                            heading_vec = await self._generate_embedding(chunk.heading)

                        content_vec = await self._generate_embedding(
                            f"{chunk.heading}\n\n{chunk.content[:1024]}"
                        )

                        doc = {
                            "id": chunk.id,
                            "chunk_id": chunk.id,
                            "source_id": chunk.source_id,
                            "source_config_id": chunk.source_config_id,
                            "rank": chunk.rank,
                            "heading": chunk.heading,
                            "content": chunk.content,
                            "heading_vector": heading_vec,
                            "content_vector": content_vec,
                            "references": chunk.references,
                            "chunk_type": "TEXT",
                            "content_length": chunk.chunk_length,
                        }
                        documents.append(doc)
                    except Exception as retry_e:
                        logger.error(f"Single vector generation failed: {chunk.id}: {retry_e}")
                        embedding_failed += 1
                        failed_chunk_ids.append(chunk.id)
                        # Record whether it is retryable
                        if is_retryable_error(retry_e):
                            logger.warning(f"Vector generation failed (retryable): {chunk.id}")
                        else:
                            logger.error(f"Vector generation failed (not retryable): {chunk.id}")

        if embedding_failed:
            # The client already retried with backoff, and a one-by-one retry happened here too; a remaining failure is a real failure.
            # Raise **before** writing the index: a chunk with no vector can never be found by search,
            # while the caller would only see "success" - that silent gap is far more dangerous than failing the whole batch.
            raise AIError(
                f"Chunk vector generation failed for {embedding_failed}/{len(chunks)} chunks, the index write was aborted"
                f" (the first few failed chunks: {', '.join(failed_chunk_ids[:5])})"
            )

        # === Stage 2: index into ES in batch ===
        indexed = 0
        es_failed = 0

        for i in range(0, len(documents), es_bulk_size):
            batch = documents[i:i+es_bulk_size]

            try:
                # Index in batch
                result = await es_client.bulk_index(
                    index=repo.INDEX_NAME,
                    documents=batch,
                    return_details=True,
                    routing=source_config_id
                )

                indexed += result["success_count"]

                # Handle the failures: retry one by one
                if result["error_count"] > 0:
                    failed_ids = {err["id"] for err in result["errors"]}
                    for doc in batch:
                        if doc["id"] in failed_ids:
                            try:
                                await es_client.index_document(
                                    index=repo.INDEX_NAME,
                                    document=doc,
                                    doc_id=doc["id"],
                                    routing=source_config_id
                                )
                                indexed += 1
                            except Exception as retry_e:
                                logger.error(f"Retry indexing failed: {doc['id']}: {retry_e}")
                                es_failed += 1

            except Exception as e:
                logger.error(f"Batch indexing failed, degrading to a retry: {e}")
                # Degrade: retry the whole batch one by one
                for doc in batch:
                    try:
                        await es_client.index_document(
                            index=repo.INDEX_NAME,
                            document=doc,
                            doc_id=doc["id"],
                            routing=source_config_id
                        )
                        indexed += 1
                    except Exception as retry_e:
                        logger.error(f"Degraded indexing failed: {doc['id']}: {retry_e}")
                        es_failed += 1
                        # Record whether it is retryable
                        if is_retryable_error(retry_e):
                            logger.warning(f"ES indexing failed (retryable): {doc['id']}")
                        else:
                            logger.error(f"ES indexing failed (not retryable): {doc['id']}")

        if es_failed:
            # Chunk đã ở trong DB nhưng không vào index: search sẽ không bao giờ thấy nó, còn
            # người dùng thấy "ingest xong". Ném lên để document đi vào FAILED thay vì xanh giả.
            raise StorageError(
                f"Chunk index write failed for {es_failed}/{len(documents)} chunks; those chunks "
                "would be stored without vectors (unsearchable)"
            )

        total_time = time.perf_counter() - start_time

        return {
            "total_chunks": len(chunks),
            "indexed_count": indexed,
            "embedding_failed": embedding_failed,
            "es_failed": es_failed,
            "embedding_batches": (len(chunks) + embedding_batch_size - 1) // embedding_batch_size,
            "es_batches": (len(documents) + es_bulk_size - 1) // es_bulk_size,
            "total_time": f"{total_time:.2f}s",
            "avg_time": f"{total_time/len(chunks):.3f}s/chunk"
        }


class DocumentLoader(BaseLoader):
    """Document loader"""

    def __init__(
        self,
        parser: Optional[MarkdownParser] = None,
        processor: Optional[DocumentProcessor] = None,
        max_tokens: Optional[int] = None,
        min_content_length: int = 100,
        merge_short_sections: bool = True,
        chunk_mode: str = "standard",
        progress_callback: Optional[Callable[[str], Optional[Awaitable[None]]]] = None,
    ) -> None:
        """
        Initialise the document loader

        Args:
            parser: the document parser (a default is used when omitted)
            processor: the document processor (a default is used when omitted)
            max_tokens: maximum token count (used to build the default parser)
            min_content_length: minimum content length (used to build the default parser)
            merge_short_sections: whether short sections are merged (used to build the default parser)
            chunk_mode: chunking mode (used to build the default parser)
        """
        # Call the parent initialiser
        super().__init__(processor=processor, progress_callback=progress_callback)

        # Create the parser (when none was given)
        if parser is not None:
            self.parser = parser
        else:
            parser_params = {}
            if max_tokens is not None:
                parser_params["max_tokens"] = max_tokens
            self.parser = MarkdownParser(**parser_params)

    async def _mark_article_parse_failed(self, article_id: Optional[str], error: str) -> None:
        """Mark the article's parse_status as EXTRACTION_FAILED (best effort)."""
        if not article_id:
            return

        try:
            async with self.session_factory() as session:
                article = await session.get(Article, article_id)
                if not article:
                    return
                article.parse_status = ArticleParseStatus.EXTRACTION_FAILED.value
                article.error = error
                await session.commit()
        except Exception as update_err:  # noqa: BLE001
            logger.warning(
                "Failed to write the article parse-failure status: article_id=%s, error=%s",
                article_id,
                update_err,
            )

    async def load(self, config: DocumentLoadConfig) -> LoadResult:
        """
        Load a document (the main entry point)

        Args:
            config: the DocumentLoadConfig object

        Returns:
            LoadResult (holding article_id and chunk_ids)

        Example:
            >>> config = DocumentLoadConfig(
            ...     source_config_id="source-uuid",
            ...     path="doc.md",
            ...     background="technical documentation"
            ... )
            >>> result = await loader.load(config)
            >>> # result.source_id, result.chunk_ids
        """
        # Save the batch processing configuration onto the instance
        self._enable_batch_indexing = config.enable_batch_indexing
        self._embedding_batch_size = config.embedding_batch_size
        self._es_bulk_index_size = config.es_bulk_index_size

        if not config.path:
            raise LoadError("File load mode needs a path")

        path = config.path if isinstance(config.path, Path) else Path(config.path)

        if not path.is_file():
            raise LoadError(f"Not a file: {path}")

        return await self.load_file(
            file_path=path,
            source_config_id=config.source_config_id,
            background=config.background or "",
            auto_vector=config.auto_vector,
            max_tokens=config.max_tokens,
            min_content_length=config.min_content_length,
            merge_short_sections=config.merge_short_sections,
            chunk_mode=config.chunk_mode,
        )

    async def load_file(
        self,
        file_path: Path,
        source_config_id: str,
        background: str = "",
        auto_vector: bool = True,
        max_tokens: Optional[int] = None,
        min_content_length: Optional[int] = None,
        merge_short_sections: Optional[bool] = None,
        chunk_mode: Optional[str] = None,
    ) -> LoadResult:
        """
        Load a document file

        Args:
            file_path: the file path
            source_config_id: source ID
            background: background information
            auto_vector: whether to index into Elasticsearch automatically
            max_tokens: maximum tokens per section
            min_content_length: minimum content length
            merge_short_sections: whether short sections are merged
            chunk_mode: chunking mode

        Returns:
            LoadResult (holding article_id and chunk_ids)

        Raises:
            LoadError: loading failed
        """
        article_id = None
        try:
            logger.info(f"Loading the document: {file_path}")

            # 1. Check the file
            if not file_path.exists():
                raise LoadError(f"The file does not exist: {file_path}")

            if not file_path.is_file():
                raise LoadError(f"Not a file: {file_path}")

            # Pre-create the Article record
            article_id = str(uuid.uuid4())
            async with self.session_factory() as session:
                article_orm = Article(
                    id=article_id,
                    source_config_id=source_config_id,
                    title=normalize_heading_text(file_path.stem) or "Untitled",
                    status="PENDING",
                )
                session.add(article_orm)
                await session.commit()

            # 2. Parse the document (per the configuration)
            await self._notify_progress("Chunking")
            parser_params = {}
            if max_tokens is not None and max_tokens != 8000:
                parser_params["max_tokens"] = max_tokens
            if chunk_mode is not None:
                parser_params["chunk_mode"] = chunk_mode

            chunking_result: Optional[ChunkingResult] = None
            if parser_params:
                # Create a temporary parser with the given parameters
                parser = MarkdownParser(**parser_params)
                content, section_count = await parser.parse_file_async(file_path)
                chunking_result = parser.get_last_chunking_result()
            else:
                content, section_count = await self.parser.parse_file_async(file_path)
                chunking_result = self.parser.get_last_chunking_result()

            logger.info(f"Document parsed, {section_count} sections")

            # 3. Extract the title
            title = self.parser.extract_title(content)

            # 4. Save into the database
            article_id, chunk_ids = await self._save_to_database(
                title=title,
                content=content,
                source_config_id=source_config_id,
                article_id=article_id,
                chunking_result=chunking_result,
            )

            logger.info(
                f"Document loaded: {title}",
                extra={
                    "article_id": article_id,
                    "chunk_count": len(chunk_ids),
                    "file_path": str(file_path),
                },
            )

            # 5. Index into Elasticsearch (optional)
            if auto_vector:
                await self._index_to_elasticsearch(article_id)

            # 6. Return the LoadResult
            return LoadResult(
                source_id=article_id,
                source_type="ARTICLE",
                chunk_ids=chunk_ids,
                source_config_id=source_config_id,
                title=title,
                chunk_count=len(chunk_ids),
                extra={
                    "file_path": str(file_path),
                    "section_count": section_count,
                }
            )

        except Exception as e:
            if article_id:
                await self._mark_article_parse_failed(article_id, str(e))
            logger.error(f"Loading the document failed: {file_path}: {e}", exc_info=True)

            # Tell retryable and non-retryable errors apart
            if is_retryable_error(e):
                logger.warning(f"Loading the document failed (retryable): {file_path}")
            else:
                logger.error(f"Loading the document failed (not retryable): {file_path}")

            if isinstance(e, LoadError):
                raise
            raise LoadError(f"Loading the document failed: {e}") from e

    async def _save_to_database(
        self,
        title: str,
        content: str,
        source_config_id: str,
        article_id: str,
        chunking_result: ChunkingResult,
        document_id_for_binding: Optional[str] = None,
    ) -> tuple[str, List[str]]:
        """
        Save the article, its SourceChunk rows and its ArticleSection rows into the database

        Args:
            title: the article title
            content: the article body (the full markdown)
            source_config_id: source ID
            article_id: article ID (the record must already exist)
            chunking_result: the chunking framework result
            document_id_for_binding: optional document ID (bound to Article.source_id)

        Returns:
            (article_id, chunk_ids)
        """
        max_retries = 3  # deadlock retry count
        batch_size = 100  # bulk insert size

        # -- Precomputed outside the transaction: build every row to insert, so no CPU-heavy work happens while holding the lock --
        all_section_data = []
        section_id_by_order: Dict[int, str] = {}

        for section_draft in chunking_result.article_sections:
            section_id = str(uuid.uuid4())
            section_id_by_order[section_draft.order_index] = section_id
            image_url = None
            if section_draft.section_type == "IMAGE":
                image_url = (section_draft.metadata or {}).get("image_src")
            section_extra_data = dict(section_draft.metadata or {})
            section_extra_data["token_count"] = max(
                0, estimate_tokens(section_draft.content or "")
            )
            all_section_data.append(
                {
                    "id": section_id,
                    "article_id": article_id,
                    "order_index": section_draft.order_index,
                    "render_group_index": section_draft.render_group_index,
                    "type": section_draft.section_type,
                    "rank": section_draft.order_index,
                    "heading": normalize_heading_text(section_draft.heading),
                    "content": section_draft.content or "",
                    "raw_content": section_draft.raw_content,
                    "image_url": image_url,
                    "length": len(section_draft.content or ""),
                    "extra_data": section_extra_data,
                }
            )

        chunk_ids = []
        all_chunk_data = []
        for chunk_draft in chunking_result.source_chunks:
            chunk_id = str(uuid.uuid4())
            chunk_ids.append(chunk_id)
            references = [
                section_id_by_order[idx]
                for idx in chunk_draft.section_order_indices
                if idx in section_id_by_order
            ]
            all_chunk_data.append(
                {
                    "id": chunk_id,
                    "source_type": "ARTICLE",
                    "source_id": article_id,
                    "source_config_id": source_config_id,
                    "article_id": article_id,
                    "heading": normalize_heading_text(chunk_draft.heading),
                    "content": chunk_draft.content,
                    "raw_content": chunk_draft.raw_content,
                    "rank": chunk_draft.rank,
                    "chunk_length": len(chunk_draft.content or ""),
                    "references": references,
                    "extra_data": chunk_draft.metadata or {},
                }
            )

        total_sentences = len(all_section_data)

        # -- Inside the transaction: DB work only, so the lock is held as briefly as possible --
        for attempt in range(max_retries):
            try:
                async with self.session_factory() as session:
                    # Check the source exists
                    source = await session.get(SourceConfig, source_config_id)
                    if not source:
                        raise LoadError(f"The source does not exist: {source_config_id}")

                    if not article_id:
                        raise LoadError("article_id cannot be empty: only updating an existing Article is supported")

                    article = await session.get(Article, article_id)
                    if not article:
                        raise LoadError(f"The article does not exist and creating one is not allowed: {article_id}")

                    # Update the existing Article
                    article.title = normalize_heading_text(title) or "Untitled"
                    article.summary = None
                    article.content = content
                    article.category = None
                    article.tags = None
                    article.error = None
                    if document_id_for_binding:
                        article.source_id = document_id_for_binding

                    # Delete the old SourceChunk and ArticleSection rows, and soft-delete the related SourceEvent rows
                    stmt_chunk = delete(SourceChunk).where(
                        SourceChunk.source_id == article_id,
                        SourceChunk.source_type == "ARTICLE"
                    )
                    await session.execute(stmt_chunk)

                    stmt_section = delete(ArticleSection).where(
                        ArticleSection.article_id == article_id)
                    await session.execute(stmt_section)

                    # Soft-delete the old events (in step with the physical ArticleSection delete)
                    await session.execute(
                        update(SourceEvent)
                        .where(
                            SourceEvent.article_id == article_id,
                            SourceEvent.not_deleted(),
                        )
                        .values(status="DELETED")
                    )

                    # Bulk insert the precomputed section and chunk rows
                    if all_section_data:
                        for i in range(0, len(all_section_data), batch_size):
                            batch = all_section_data[i:i + batch_size]
                            stmt = insert(ArticleSection).values(batch)
                            await session.execute(stmt)

                    if all_chunk_data:
                        for i in range(0, len(all_chunk_data), batch_size):
                            batch = all_chunk_data[i:i + batch_size]
                            stmt = insert(SourceChunk).values(batch)
                            await session.execute(stmt)

                    await session.commit()

                    logger.info(
                        f"Article saved",
                        extra={
                            "article_id": article.id,
                            "chunk_count": len(chunk_ids),
                            "total_sentences": total_sentences,
                        },
                    )

                    return article.id, chunk_ids

            except OperationalError as e:
                err_msg = str(e)
                is_retryable = "Deadlock" in err_msg or "Lock wait timeout" in err_msg
                if is_retryable and attempt < max_retries - 1:
                    wait_time = 1.0 * (2 ** attempt)  # exponential backoff: 1s, 2s, 4s
                    logger.warning(
                        f"Database lock conflict (retryable), retrying in {wait_time}s (attempt {attempt + 1}/{max_retries}): {err_msg}"
                    )
                    await asyncio.sleep(wait_time)
                    continue
                # A non-retryable database error
                logger.error(f"The database operation failed (not retryable): {err_msg}")
                raise

    async def _index_to_elasticsearch(self, article_id: str) -> None:
        """
        Index the article's SourceChunk rows into Elasticsearch

        Args:
            article_id: article ID (UUID)
        """
        # Call the parent's shared indexing method
        await self._index_source_chunks_to_es(article_id, "ARTICLE")
