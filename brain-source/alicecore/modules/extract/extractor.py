"""
Event extractor

Main controller - coordinates the extraction flow

Flow: chunks -> processor(LLM) -> filter -> parser -> saver
"""

import asyncio
import inspect
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Awaitable, Callable, Dict, List, Optional, Union

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from alicecore.core.ai.base import BaseLLMClient
from alicecore.core.prompt.manager import PromptManager
from alicecore.db import (
    ArticleParseStatus,
    SourceChunk,
    EntityType as DBEntityType,
    EventEntity,
    SourceEvent,
    get_session_factory,
)
from alicecore.exceptions import ExtractError
from alicecore.modules.extract.config import ExtractConfig
from alicecore.modules.extract.processor import EventProcessor
from alicecore.modules.extract.parser import ResultParser, ParseContext
from alicecore.modules.extract.saver import EventSaver
from alicecore.utils import get_logger, get_utc_now

logger = get_logger("extract.extractor")


class EventExtractor:
    """
    Event extractor (main controller)

    Flow: chunks -> processor(LLM) -> filter -> parser -> saver
    """

    def __init__(
        self,
        prompt_manager: PromptManager,
        model_config: Optional[Dict] = None,
        on_progress: Optional[Callable[[int, int], Union[Awaitable[None], None]]] = None,
    ):
        """
        Initialise the event extractor

        Args:
            prompt_manager: the prompt manager
            model_config: LLM configuration dictionary (optional)
            on_progress: progress callback (completed, total)
        """
        self.prompt_manager = prompt_manager
        self.model_config = model_config
        self._on_progress = on_progress
        self._llm_client = None  # initialised lazily
        self.session_factory = get_session_factory()
        self.logger = get_logger("extract.extractor")

        # Components (initialised lazily)
        self._saver = None
        self._parser = None

    async def _get_llm_client(self) -> BaseLLMClient:
        """Get the LLM client (lazy)"""
        if self._llm_client is None:
            from alicecore.core.ai.factory import create_llm_client

            self._llm_client = await create_llm_client(
                scenario="extract", model_config=self.model_config
            )

        return self._llm_client

    async def extract(self, config: ExtractConfig) -> List[SourceEvent]:
        """
        Extract events (the single entry point of the new architecture)

        Flow:
        1. load every chunk
        2. process them with max_concurrency in parallel (guarded by a Semaphore)
        3. one ExtractorAgent handles each chunk
        4. merge every result
        5. save into the database + Elasticsearch
        6. mark the source finished

        Args:
            config: the extraction configuration

        Returns:
            The list of events extracted from every chunk

        Example:
            config = ExtractConfig(
                source_config_id="source-uuid",
                chunk_ids=["chunk-1", "chunk-2", "chunk-3"],
                max_concurrency=3
            )
            events = await extractor.extract(config)
        """
        self.logger.info(
            f"Batch extraction started: chunks={len(config.chunk_ids)}, " f"concurrency={config.max_concurrency}"
        )

        sync_date_value = datetime.now(timezone.utc).replace(tzinfo=None)

        try:
            # 1. Load every chunk
            chunks = await self._load_chunks(config.chunk_ids)

            if not chunks:
                self.logger.info("No usable chunk was found")
                return []

            # 2. Mark it running
            await self._update_source_status(chunks, status="EXTRACTING")

            # 3. Process the chunks concurrently (one agent per chunk)
            all_events = await self._process_chunks_with_agents(chunks, config)

            self.logger.info(f"Batch extraction finished: chunks={len(chunks)}, events={len(all_events)}")

            # 4. Reorder by the original text order and assign a global rank
            if all_events:
                # Build the chunk_id -> chunk.rank map
                chunk_rank_map = {chunk.id: chunk.rank for chunk in chunks}

                # Ordering rules:
                # 1. chunk.rank first (keeps the order between chunks)
                # 2. then the event time (conversation) or the rank inside the chunk (document)
                def sort_key(event):
                    chunk_order = chunk_rank_map.get(event.chunk_id, 9999)
                    event_order = event.rank or 0
                    return (chunk_order, event_order)

                all_events.sort(key=sort_key)

                # Reassign a globally continuous rank
                for i, event in enumerate(all_events):
                    event.rank = i

                self.logger.info(
                    f"Events reordered to match the original text: chunks={len(chunks)}, events={len(all_events)}"
                )

            # 5. Save into the database (Elasticsearch included)
            if all_events:
                await self._save_events(all_events, config)

                # 6. Reload the events from the database (with the full relation data)
                event_ids = [e.id for e in all_events]
                all_events = await self._reload_events_with_relations(event_ids)
            else:
                self.logger.info("No event was extracted, skipping the save")

            # 7. Mark the source finished and write sync_date
            if chunks:
                await self._update_source_status(
                    chunks, status="COMPLETED", sync_date=sync_date_value
                )

            return all_events

        except Exception as e:
            # On failure, mark the status failed and write sync_date
            self.logger.error(f"Extraction failed: {e}", exc_info=True)
            try:
                chunks = await self._load_chunks(config.chunk_ids)
                if chunks:
                    await self._update_source_status(
                        chunks, status="FAILED", error=str(e), sync_date=sync_date_value
                    )
            except Exception as update_error:
                self.logger.error(f"Error while writing the failure status: {update_error}")

            raise ExtractError(f"Extraction failed: {e}") from e
        finally:
            # Make sure the resources are released
            if self._saver is not None and hasattr(self._saver, '_es_client'):
                if self._saver._es_client is not None:
                    try:
                        await self._saver._es_client.client.close()
                        self._saver._es_client = None
                        self._saver._event_repo = None
                        self._saver._entity_repo = None
                    except Exception as cleanup_err:
                        self.logger.warning(f"Failed to clean up the ES client: {cleanup_err}")

    async def _load_chunks(self, chunk_ids: List[str]) -> List[SourceChunk]:
        """Load the chunks in batch (ordered by rank)"""
        async with self.session_factory() as session:
            result = await session.execute(
                select(SourceChunk)
                .where(SourceChunk.id.in_(chunk_ids))
                .order_by(SourceChunk.rank)  # ordered by rank
            )
            chunks = list(result.scalars().all())

            if len(chunks) != len(chunk_ids):
                missing = set(chunk_ids) - {c.id for c in chunks}
                self.logger.info(f"Some chunks do not exist: {missing}")

            return chunks

    async def _process_chunks_with_agents(
        self, chunks: List[SourceChunk], config: ExtractConfig
    ) -> List[SourceEvent]:
        """
        Process the chunks concurrently (one agent per chunk)

        An asyncio.Semaphore bounds the concurrency:
        - at most max_concurrency agents run at once
        - the remaining chunks queue automatically
        - as soon as one chunk finishes, the next starts

        Args:
            chunks: the chunk list
            config: the extraction configuration

        Returns:
            Every event, merged
        """
        semaphore = asyncio.Semaphore(config.max_concurrency)

        # Progress tracking
        completed = 0
        success_count = 0
        failed_count = 0
        total = len(chunks)
        lock = asyncio.Lock()

        async def process_single_chunk(chunk: SourceChunk, index: int) -> List[SourceEvent]:
            """Process one chunk (bounded concurrency and progress accounting)"""
            nonlocal completed, success_count, failed_count

            async with semaphore:  # take a concurrency slot (wait when there is none)
                is_success = False
                events = []
                error_msg = None

                try:
                    self.logger.info(
                        f"[{index+1}/{total}] Processing: chunk_id={chunk.id}, "
                        f"type={chunk.source_type}"
                    )

                    # Call the chunk-level extraction (through ExtractorAgent)
                    events = await self.extract_from_chunk(chunk, config)
                    is_success = True

                except Exception as e:
                    error_msg = str(e)
                    self.logger.error(
                        f"[{index+1}/{total}] failed: "
                        f"chunk_id={chunk.id}, error={e}",
                        exc_info=True,
                    )

                # Update the progress (counting inside the lock, the callback outside it)
                async with lock:
                    completed += 1
                    if is_success:
                        success_count += 1
                    else:
                        failed_count += 1
                    progress = completed * 100 // total
                    should_report = self._on_progress and (completed % 10 == 0 or completed == total)
                    snap_completed, snap_total = completed, total

                if is_success:
                    self.logger.info(
                        f"[{index+1}/{total}] done ({progress}%): "
                        f"chunk_id={chunk.id}, events={len(events)}"
                    )
                else:
                    self.logger.error(
                        f"[{index+1}/{total}] failed ({progress}%): "
                        f"chunk_id={chunk.id}"
                    )

                if should_report:
                    try:
                        result = self._on_progress(snap_completed, snap_total)
                        if inspect.isawaitable(result):
                            await result
                    except Exception as e:
                        self.logger.warning(f"The progress callback failed: {e}")

                return events if is_success else []
                # The slot is released automatically on the way out

        # Run every chunk concurrently
        self.logger.info(f"Concurrent extraction started: total={total}, concurrency={config.max_concurrency}")

        tasks = [process_single_chunk(chunk, i) for i, chunk in enumerate(chunks)]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        # Merge the results
        all_events = []
        for events in results:
            all_events.extend(events)

        # Final statistics
        self.logger.info(
            f"Batch extraction statistics: total={total}, succeeded={success_count}, "
            f"failed={failed_count}, events={len(all_events)}"
        )

        return all_events

    async def _save_events(
        self, events: List[SourceEvent], config: ExtractConfig
    ) -> List[SourceEvent]:
        """
        Save the events (through EventSaver)

        Args:
            events: the event list
            config: the extraction configuration

        Returns:
            The saved event list (with the full relation data)
        """
        if self._saver is None:
            self._saver = EventSaver()

        return await self._saver.commit(events, config)

    async def extract_from_chunk(
        self, chunk: SourceChunk, config: ExtractConfig
    ) -> List[SourceEvent]:
        """
        Extract events from a chunk

        Args:
            chunk: the source chunk object
            config: the extraction configuration

        Returns:
            The extracted event list
        """
        try:
            self.logger.info(f"Extraction from a chunk started: chunk_id={chunk.id}, type={chunk.source_type}")

            # 0. Content length filter
            content_length = chunk.chunk_length or len(chunk.content or "")
            if config.chunk_min_length > 0 and content_length < config.chunk_min_length:
                self.logger.info(f"Chunk {chunk.id} content is too short ({content_length} characters), skipping")
                return []

            # 1. Load the content and metadata
            content_items, raw_metadata = await self._load_chunk_content(chunk, config)
            if not content_items:
                self.logger.info(f"Chunk {chunk.id} has no content (it may be all images, which were filtered out)")
                return []

            # 2. Load the entity types
            entity_types = await self._load_entity_types_for_chunk(config)

            # 3. Create the EventProcessor
            llm_client = await self._get_llm_client()
            processor = EventProcessor(
                llm_client=llm_client,
                prompt_manager=self.prompt_manager,
                config=config,
            )
            await processor.initialize(entity_types)

            # 4. Build the metadata
            metadata = {
                "document_title": raw_metadata.get("title", ""),
                "document_summary": raw_metadata.get("summary", ""),  # whole-document summary (the global view)
                "chunk_title": chunk.heading or f"Section {chunk.rank + 1}",
                "previous_context": self._format_previous_context(
                    raw_metadata.get("previous_chunk")
                ),
            }

            # 5. Call the LLM extraction
            raw_result = await processor.process(
                items=content_items,
                metadata=metadata,
                source_type=chunk.source_type,
            )

            # 6. Parse the result (Dict -> SourceEvent)
            if self._parser is None:
                self._parser = ResultParser(config)

            # Build the parsing context
            context = ParseContext(
                source_config_id=config.source_config_id,
                source_type=chunk.source_type,
                source_id=chunk.source_id,
                chunk_id=chunk.id,
                source_created_time=await processor.get_source_created_time(
                    content_items, chunk.source_type
                ),
            )

            # Parse the events
            raw_items = raw_result.get("data", {}).get("items", [])
            events = self._parser.parse_events(raw_items, content_items, context)

            # 7. Handle the entity associations
            events = await self._parser.process_entity_associations(events, entity_types)

            self.logger.info(f"Chunk extraction finished: chunk_id={chunk.id}, events={len(events)}")
            return events

        except Exception as e:
            self.logger.error(f"Chunk extraction failed: {e}", exc_info=True)
            raise ExtractError(f"Chunk extraction failed: {e}") from e

    def _format_previous_context(self, previous_chunk) -> str:
        """Format the preceding context"""
        if not previous_chunk:
            return ""

        title = previous_chunk.get("heading") or previous_chunk.get("title") or "Preceding text"
        content = previous_chunk.get("content", "")

        if len(content) > 300:
            content = content[:300] + "..."

        return f"**{title}**\n{content}"

    async def _load_chunk_content(self, chunk: SourceChunk, config: ExtractConfig):
        """Load the content and metadata of a chunk"""
        if chunk.source_type == "ARTICLE":
            return await self._load_article_content(chunk, config)
        else:
            raise ExtractError(f"Unsupported type: {chunk.source_type}")

    async def _load_article_content(self, chunk: SourceChunk, config: ExtractConfig):
        """Load the article sections plus the preceding chunk's content as background"""
        from alicecore.db import Article, ArticleSection

        async with self.session_factory() as session:
            # 1. Load the article (source background)
            article = await session.get(Article, chunk.source_id)
            if not article:
                raise ExtractError(f"The article does not exist: {chunk.source_id}")

            # 2. Load this chunk's sections (the content to process)
            section_ids = chunk.references if chunk.references else []

            if section_ids:
                query = select(ArticleSection).where(ArticleSection.id.in_(section_ids))
                # Filter out image sections (they only add noise)
                # Note: type may be NULL, so or_ is needed
                if config.filter_image_sections:
                    query = query.where(
                        or_(ArticleSection.type.is_(None), ArticleSection.type != "IMAGE")
                    )
                sections_result = await session.execute(query.order_by(ArticleSection.rank))
            else:
                query = select(ArticleSection).where(ArticleSection.article_id == chunk.source_id)
                # Filter out image sections (they only add noise)
                # Note: type may be NULL, so or_ is needed
                if config.filter_image_sections:
                    query = query.where(
                        or_(ArticleSection.type.is_(None), ArticleSection.type != "IMAGE")
                    )
                sections_result = await session.execute(query.order_by(ArticleSection.rank))

            sections = list(sections_result.scalars().all())

            # 3. Load the previous chunk's content (preceding background)
            previous_chunk = None
            if chunk.rank > 0:
                prev_result = await session.execute(
                    select(SourceChunk)
                    .where(SourceChunk.source_id == chunk.source_id)
                    .where(SourceChunk.source_type == "ARTICLE")
                    .where(SourceChunk.rank == chunk.rank - 1)
                )
                previous_chunk = prev_result.scalars().first()  # first() avoids a multi-row error

            return sections, {
                # Article table fields (source background)
                "title": article.title,
                "summary": article.summary,
                # Current chunk information
                "chunk_rank": chunk.rank,
                "chunk_heading": chunk.heading,
                # Preceding chunk (supplies context)
                "previous_chunk": (
                    {
                        "heading": previous_chunk.heading,
                        "content": (
                            previous_chunk.content[:800]
                            if len(previous_chunk.content or "") > 800
                            else previous_chunk.content
                        ),
                    }
                    if previous_chunk and previous_chunk.content
                    else None
                ),
            }

    async def _load_entity_types_for_chunk(self, config: ExtractConfig) -> List[DBEntityType]:
        """
        Load entity type definitions (always returns non-empty list)

        Priority:
        1. Default global types (is_default=True)
        2. Source-level custom types
        3. Runtime types in config (converted to DBEntityType objects)

        Returns:
            List of DBEntityType objects
        """
        entity_types: List[DBEntityType] = []

        async with self.session_factory() as session:
            # Load the default types
            default_result = await session.execute(
                select(DBEntityType)
                .where(DBEntityType.is_default == True)
                .where(DBEntityType.is_active == True)
            )
            default_types = default_result.scalars().all()
            entity_types.extend(default_types)

            # Load the source-level types
            if config.source_config_id:
                custom_result = await session.execute(
                    select(DBEntityType)
                    .where(DBEntityType.source_config_id == config.source_config_id)
                    .where(DBEntityType.is_active == True)
                )
                custom_types = custom_result.scalars().all()
                entity_types.extend(custom_types)

        # Runtime types (highest priority) - must be converted into DBEntityType objects
        if config.custom_entity_types:
            async with self.session_factory() as session:
                for custom_et in config.custom_entity_types:
                    existing_result = await session.execute(
                        select(DBEntityType)
                        .where(DBEntityType.type == custom_et.type)
                        .where(
                            (DBEntityType.source_config_id == config.source_config_id)
                            | (DBEntityType.is_default == True)
                        )
                        .where(DBEntityType.is_active == True)
                    )
                    existing = existing_result.scalar_one_or_none()

                    if existing:
                        entity_types.append(existing)
                    else:
                        value_constraints = getattr(custom_et, "value_constraints", None)
                        if not value_constraints:
                            validation_rule = getattr(custom_et, "validation_rule", None)
                            if validation_rule:
                                value_constraints = validation_rule

                        temp_et = DBEntityType(
                            id=str(uuid.uuid4()),
                            source_config_id=config.source_config_id,
                            scope="source",
                            type=custom_et.type,
                            name=custom_et.name,
                            description=custom_et.description or "",
                            weight=Decimal(str(custom_et.weight)),
                            similarity_threshold=Decimal("0.800"),
                            value_constraints=value_constraints,
                            extra_data=(
                                {
                                    "extraction_prompt": getattr(
                                        custom_et, "extraction_prompt", None
                                    ),
                                    "extraction_examples": getattr(
                                        custom_et, "extraction_examples", None
                                    ),
                                }
                                if (
                                    hasattr(custom_et, "extraction_prompt")
                                    or hasattr(custom_et, "extraction_examples")
                                )
                                else None
                            ),
                            is_default=False,
                            is_active=True,
                        )
                        entity_types.append(temp_et)

        # Deduplicate by type, keeping only the first occurrence of each entity type
        seen: set = set()
        deduped: List[DBEntityType] = []
        for et in entity_types:
            if et.type not in seen:
                seen.add(et.type)
                deduped.append(et)
        entity_types = deduped

        # Log the entity types finally loaded (for debugging)
        entity_type_names = [et.type for et in entity_types]
        self.logger.info(
            f"Entity types loaded: {len(entity_types)} types - {entity_type_names}"
        )

        return entity_types

    async def _reload_events_with_relations(self, event_ids: List[str]) -> List[SourceEvent]:
        """
        Reload events from database with relations preloaded

        Solves cross-session issue: re-query after save to ensure all relations are loaded correctly

        Args:
            event_ids: List of event IDs

        Returns:
            List of events with complete relations
        """
        if not event_ids:
            return []

        async with self.session_factory() as session:
            result = await session.execute(
                select(SourceEvent)
                .where(SourceEvent.id.in_(event_ids))
                .options(
                    selectinload(SourceEvent.event_associations).selectinload(EventEntity.entity)
                )
            )
            events = list(result.scalars().all())

            # expire_on_commit=False makes the data readable outside the session
            session.expire_on_commit = False

            # Trigger relation loading (so every field is readable outside the session)
            for event in events:
                # Trigger event field loading
                _ = event.title
                _ = event.created_time

                # Trigger association and entity loading
                if hasattr(event, "event_associations"):
                    for assoc in event.event_associations:
                        _ = assoc.id
                        if assoc.entity:
                            _ = assoc.entity.name
                            _ = assoc.entity.type

            self.logger.info(f"Reloaded {len(events)} events (with the full relations)")
            return events

    async def _update_source_status(
        self,
        chunks: List[SourceChunk],
        status: str,
        error: Optional[str] = None,
        sync_date: Optional[datetime] = None,
    ) -> None:
        """
        Update source status (Article)

        Args:
            chunks: chunk list (used to determine source type and ID)
            status: internal status value (EXTRACTING/COMPLETED/FAILED)
            error: error message (optional, provided on failure)
            sync_date: sync time (UTC time pre-fetched before extraction starts, written on completion/failure)
        """
        if not chunks:
            return

        from alicecore.db import Article

        # Determine the source type and ID (one batch of chunks should come from one source)
        source_type = chunks[0].source_type
        source_id = chunks[0].source_id

        # Verify every chunk comes from the same source
        if not all(c.source_type == source_type and c.source_id == source_id for c in chunks):
            self.logger.info("The chunks come from different sources, the status cannot be updated in one go")
            return

        async with self.session_factory() as session:
            try:
                if source_type == "ARTICLE":
                    result = await session.execute(select(Article).where(Article.id == source_id))
                    source = result.scalar_one_or_none()

                    if source:
                        source.status = status
                        if status == "EXTRACTING":
                            source.parse_status = ArticleParseStatus.EXTRACTING.value
                        elif status == "COMPLETED":
                            source.parse_status = ArticleParseStatus.COMPLETED.value
                            source.sync_date = get_utc_now().replace(tzinfo=None)
                        elif status == "FAILED":
                            source.parse_status = ArticleParseStatus.EXTRACTION_FAILED.value
                        if error:
                            source.error = error
                        await session.commit()
                        self.logger.info(f"Article status updated: {source_id} -> {status}")
                    else:
                        self.logger.info(f"The article does not exist: {source_id}")

                else:
                    self.logger.info(f"Unsupported source type: {source_type}")

            except Exception as e:
                self.logger.error(f"Failed to update the source status: {e}", exc_info=True)
