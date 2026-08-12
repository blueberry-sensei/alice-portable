"""
Event saver - owns persistence

Responsibilities:
- save events into the database (MySQL)
- sync them into the vector store (Elasticsearch)
- batch processing optimisation
"""

import time
import struct
from typing import Any, Dict, List

from sqlalchemy import select, text, update
from sqlalchemy.orm import selectinload

from alicecore.core.ai.factory import get_embedding_client
from alicecore.core.config import get_settings
from alicecore.core.storage.client import get_es_client
from alicecore.core.storage.repositories.entity_repository import EntityVectorRepository
from alicecore.core.storage.repositories.event_repository import EventVectorRepository
from alicecore.db import Entity, EventEntity, EventEntityEmbedding, SourceEvent, get_session_factory
from alicecore.exceptions import AIError, StorageError
from alicecore.modules.extract.config import ExtractConfig
from alicecore.utils import get_logger, is_retryable_error

logger = get_logger("extract.saver")


EVENT_ENTITY_EMBEDDING_TABLE_NAME = "event_entity_embedding"
EVENT_ENTITY_EMBEDDING_TRUNCATE_DIMS = 128


def to_event_entity_embedding_vec_bytes(embedding: List[float]) -> bytes:
    """
    Truncate the embedding to 128 dimensions and convert it to float32 bytes (512 bytes)
    """
    if len(embedding) < EVENT_ENTITY_EMBEDDING_TRUNCATE_DIMS:
        raise ValueError(
            f"embedding dims too short: got={len(embedding)} "
            f"need>={EVENT_ENTITY_EMBEDDING_TRUNCATE_DIMS}"
        )
    truncated = [float(x) for x in embedding[:EVENT_ENTITY_EMBEDDING_TRUNCATE_DIMS]]
    return struct.pack(f"<{EVENT_ENTITY_EMBEDDING_TRUNCATE_DIMS}f", *truncated)


class EventSaver:
    """Event saver - owns persistence"""

    def __init__(self):
        """Initialise the saver"""
        self.session_factory = get_session_factory()
        self.logger = get_logger("extract.saver")
        self.settings = get_settings()

        # Vector store (initialised lazily)
        self._es_client = None
        self._event_repo = None
        self._entity_repo = None

    async def commit(self, events: List[SourceEvent], config: ExtractConfig) -> List[SourceEvent]:
        """
        Commit the events (save into the DB + sync the vector store)

        Args:
            events: the event list (already parsed, entity associations included)
            config: the extraction configuration

        Returns:
            The saved event list (with the full relation data)
        """
        if not events:
            self.logger.info("There is no event to commit")
            return []

        self.logger.info(f"Committing {len(events)} events")

        # 1. Save into MySQL
        event_ids = await self._save_to_database(events)

        # 2. Reload (with the full relation data)
        fresh_events = await self._load_events_with_relations(event_ids)

        # 3. In a SAAS environment write event_entity_embedding
        if self._should_sync_event_entity_embedding():
            await self._sync_event_entity_embeddings_to_db(fresh_events, config)
        else:
            self.logger.info(
                f"Skipping the {EVENT_ENTITY_EMBEDDING_TABLE_NAME} write: "
                f"SERVER_TYPE={self.settings.server_type}"
            )

        # 4. Sync into the vector store (when enabled)
        if config.enable_event_vector_sync:
            await self._sync_to_vector_store(fresh_events, config)

        self.logger.info(f"Commit finished: {len(fresh_events)} events")
        return fresh_events

    def _should_sync_event_entity_embedding(self) -> bool:
        """Write the event entity vector table only in a SAAS environment"""
        return self.settings.server_type == "SAAS"

    def _collect_event_entities(self, events: List[SourceEvent]) -> List[EventEntity]:
        """Collect every event-entity association of the events"""
        event_entities: List[EventEntity] = []
        for event in events:
            if hasattr(event, "event_associations") and event.event_associations:
                event_entities.extend(event.event_associations)
        return event_entities

    async def _sync_event_entity_embeddings_to_db(
        self, events: List[SourceEvent], config: ExtractConfig
    ) -> Dict[str, Any]:
        """
        Sync the event-entity association embeddings into the MySQL table event_entity_embedding (SAAS only)
        """
        if not events:
            return {"total": 0, "upserted": 0}

        event_entities = self._collect_event_entities(events)
        if not event_entities:
            self.logger.info(f"There is no event-entity association to write into {EVENT_ENTITY_EMBEDDING_TABLE_NAME}")
            return {"total": 0, "upserted": 0}

        start_time = time.perf_counter()
        embedding_client = await get_embedding_client(scenario="general")

        rows_to_upsert: List[Dict[str, Any]] = []
        embedding_failed = 0
        assoc_by_id = {assoc.id: assoc for assoc in event_entities}

        for i in range(0, len(event_entities), config.embedding_batch_size):
            batch = event_entities[i : i + config.embedding_batch_size]
            texts = [assoc.description or f"{assoc.event_id}-{assoc.entity_id}" for assoc in batch]

            try:
                vectors = await embedding_client.batch_generate(texts)
                expected_count = len(batch)
                actual_count = len(vectors)
                if actual_count != expected_count:
                    missing_count = max(0, expected_count - actual_count)
                    extra_count = max(0, actual_count - expected_count)
                    self.logger.error(
                        "Batch event entity vector count mismatch: "
                        f"expected={expected_count}, actual={actual_count}, "
                        f"missing={missing_count}, extra={extra_count}; "
                        "degrading to a one-by-one retry"
                    )
                    raise ValueError(
                        "batch_generate returned mismatched vector count: "
                        f"expected={expected_count}, actual={actual_count}"
                    )
                for assoc, vector in zip(batch, vectors):
                    try:
                        rows_to_upsert.append(
                            {"id": assoc.id, "vec": to_event_entity_embedding_vec_bytes(vector)}
                        )
                    except Exception as pack_error:
                        self.logger.error(
                            f"Vector conversion failed {assoc.id}: {pack_error}"
                        )
                        embedding_failed += 1

            except Exception as batch_error:
                self.logger.warning(f"Batch event entity vector generation failed, degrading to a retry: {batch_error}")
                for assoc in batch:
                    try:
                        text_for_vec = assoc.description or f"{assoc.event_id}-{assoc.entity_id}"
                        vector = await embedding_client.generate(text_for_vec)
                        rows_to_upsert.append(
                            {"id": assoc.id, "vec": to_event_entity_embedding_vec_bytes(vector)}
                        )
                    except Exception as retry_error:
                        self.logger.error(f"Single event entity vector generation failed {assoc.id}: {retry_error}")
                        embedding_failed += 1

        if not rows_to_upsert:
            stats = {
                "total": len(event_entities),
                "upserted": 0,
                "embedding_failed": embedding_failed,
                "db_failed": 0,
                "time": f"{(time.perf_counter() - start_time):.2f}s",
            }
            if embedding_failed:
                # Not one succeeded, and the cause was vector generation - this is not "nothing to write", it is a total failure.
                # It must raise, otherwise the caller treats a result with missing vectors as a success.
                raise AIError(
                    f"Every {EVENT_ENTITY_EMBEDDING_TABLE_NAME} vector failed to generate: "
                    f"{embedding_failed}/{len(event_entities)} rows, nothing can be written"
                )
            self.logger.info(f"Nothing was written to {EVENT_ENTITY_EMBEDDING_TABLE_NAME}: {stats}")
            return stats

        # Dialect-aware upsert: MySQL uses ON DUPLICATE KEY, PostgreSQL/SQLite use ON CONFLICT
        from alicecore.db.base import get_engine

        _dialect = get_engine().dialect.name
        if _dialect == "mysql":
            upsert_sql = text(
                f"INSERT INTO {EVENT_ENTITY_EMBEDDING_TABLE_NAME} (id, vec) "
                f"VALUES (:id, :vec) ON DUPLICATE KEY UPDATE vec = VALUES(vec)"
            )
        else:
            upsert_sql = text(
                f"INSERT INTO {EVENT_ENTITY_EMBEDDING_TABLE_NAME} (id, vec) "
                f"VALUES (:id, :vec) ON CONFLICT (id) DO UPDATE SET vec = EXCLUDED.vec"
            )

        upserted = 0
        db_failed = 0
        for i in range(0, len(rows_to_upsert), config.index_batch_size):
            batch = rows_to_upsert[i : i + config.index_batch_size]
            try:
                async with self.session_factory() as session:
                    await session.execute(upsert_sql, batch)
                    await session.commit()
                upserted += len(batch)
            except Exception as batch_db_error:
                self.logger.error(f"Batch write into {EVENT_ENTITY_EMBEDDING_TABLE_NAME} failed, degrading to a retry: {batch_db_error}")
                for row in batch:
                    try:
                        async with self.session_factory() as session:
                            await session.execute(upsert_sql, row)
                            await session.commit()
                        upserted += 1
                    except Exception as row_db_error:
                        self.logger.error(
                            f"Single write into {EVENT_ENTITY_EMBEDDING_TABLE_NAME} failed {row['id']}: {row_db_error}"
                        )
                        db_failed += 1

        # Eventual-consistency safety net: verify this batch really landed, and compensate once per missing row
        expected_ids = set(assoc_by_id.keys())
        existing_ids = set()
        expected_id_list = list(expected_ids)
        for i in range(0, len(expected_id_list), config.index_batch_size):
            id_batch = expected_id_list[i : i + config.index_batch_size]
            async with self.session_factory() as session:
                result = await session.execute(
                    select(EventEntityEmbedding.id).where(EventEntityEmbedding.id.in_(id_batch))
                )
                existing_ids.update(result.scalars().all())

        missing_ids = expected_ids - existing_ids
        recovered = 0
        if missing_ids:
            self.logger.warning(
                f"{EVENT_ENTITY_EMBEDDING_TABLE_NAME} found {len(missing_ids)} rows missing, starting the compensating retry"
            )
            for missing_id in missing_ids:
                assoc = assoc_by_id.get(missing_id)
                if assoc is None:
                    continue
                try:
                    text_for_vec = assoc.description or f"{assoc.event_id}-{assoc.entity_id}"
                    vector = await embedding_client.generate(text_for_vec)
                    row = {"id": assoc.id, "vec": to_event_entity_embedding_vec_bytes(vector)}
                    async with self.session_factory() as session:
                        await session.execute(upsert_sql, row)
                        await session.commit()
                    recovered += 1
                except Exception as recover_error:
                    self.logger.error(
                        f"Compensating write into {EVENT_ENTITY_EMBEDDING_TABLE_NAME} failed {missing_id}: {recover_error}"
                    )

            # Verify again after compensating; still missing means raise, so nothing is lost silently
            async with self.session_factory() as session:
                result = await session.execute(
                    select(EventEntityEmbedding.id).where(
                        EventEntityEmbedding.id.in_(list(expected_ids))
                    )
                )
                existing_after_recover = set(result.scalars().all())
            final_missing_ids = expected_ids - existing_after_recover
            if final_missing_ids:
                raise RuntimeError(
                    f"{EVENT_ENTITY_EMBEDDING_TABLE_NAME} write is incomplete: "
                    f"expected={len(expected_ids)}, actual={len(existing_after_recover)}, "
                    f"missing={len(final_missing_ids)}"
                )
        else:
            final_missing_ids = set()

        total_time = time.perf_counter() - start_time
        stats = {
            "total": len(event_entities),
            "upserted": upserted,
            "embedding_failed": embedding_failed,
            "db_failed": db_failed,
            "recovered": recovered,
            "missing_after_verify": len(final_missing_ids),
            "time": f"{total_time:.2f}s",
        }
        if embedding_failed > 0 or db_failed > 0:
            self.logger.warning(f"{EVENT_ENTITY_EMBEDDING_TABLE_NAME} write partly failed: {stats}")
        else:
            self.logger.info(
                f"{EVENT_ENTITY_EMBEDDING_TABLE_NAME} write succeeded: "
                f"{upserted}/{len(event_entities)} rows in {total_time:.2f}s"
            )
        return stats

    async def _save_to_database(self, events: List[SourceEvent]) -> List[str]:
        """
        Save the events into the database

        Args:
            events: the event list

        Returns:
            The event ID list
        """
        event_ids = []

        # Collect the article_id values that need a soft delete (deduplicated)
        article_ids = {e.article_id for e in events if e.article_id}

        async with self.session_factory() as session:
            # Soft-delete the old events first, then write the new ones, in one transaction for consistency
            # This stops a service restart from leaving old events behind when extraction runs in parallel more than once
            for aid in article_ids:
                await session.execute(
                    update(SourceEvent)
                    .where(
                        SourceEvent.article_id == aid,
                        SourceEvent.not_deleted(),
                    )
                    .values(status="DELETED")
                )

            for event in events:
                event_ids.append(event.id)
                session.add(event)

                # Add the entity associations
                if hasattr(event, "event_associations") and event.event_associations:
                    for assoc in event.event_associations:
                        session.add(assoc)

            await session.commit()

        self.logger.info(f"Saved {len(event_ids)} events into the database")
        return event_ids

    async def _load_events_with_relations(self, event_ids: List[str]) -> List[SourceEvent]:
        """
        Load the events from the database (relations preloaded)

        Args:
            event_ids: the event ID list

        Returns:
            The event list (with the full relation data)
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

            # Make the data readable outside the session
            session.expire_on_commit = False

            # Trigger relation loading
            for event in events:
                _ = event.created_time
                _ = event.updated_time
                if hasattr(event, "event_associations"):
                    for assoc in event.event_associations:
                        _ = assoc.id
                        if assoc.entity:
                            _ = assoc.entity.name
                            _ = assoc.entity.type

            return events

    async def _sync_to_vector_store(self, events: List[SourceEvent], config: ExtractConfig) -> None:
        """
        Sync the events and entities into the vector store

        Args:
            events: the event list
            config: the extraction configuration
        """
        self.logger.info(f"Syncing {len(events)} events into the vector store")

        # Initialise the vector store client (lazily)
        if self._es_client is None:
            self._es_client = get_es_client()
            self._event_repo = EventVectorRepository(self._es_client.client)
            self._entity_repo = EntityVectorRepository(self._es_client.client)

        # Check the connection.
        # Bỏ qua im lặng ở đây nghĩa là event/entity vào DB mà không có vector: search không thấy
        # chúng, còn caller vẫn nhận "commit xong". Hỏng phải nhìn thấy được — ném lên.
        if not await self._es_client.ping():
            raise StorageError(
                "The vector store is unreachable, so events would be stored without vectors "
                "(unsearchable). Aborting instead of reporting a false success."
            )

        # Collect every unique entity
        unique_entities = await self._collect_unique_entities(events)

        # 1. Sync the entities (when enabled)
        if unique_entities and config.enable_entity_vector_sync:
            await self._sync_entities(list(unique_entities.values()), config)
        elif unique_entities:
            self.logger.info(
                f"Skipping the vector sync of {len(unique_entities)} entities (enable_entity_vector_sync=False)"
            )

        # 2. Sync the events
        await self._sync_events(events, config)

        # 3. Sync the event-entity associations (when enabled)
        if config.enable_event_entity_vector_sync:
            await self._sync_event_entities(events, config)

        # Status counters
        entity_status = (
            f"{len(unique_entities)} entities"
            if config.enable_entity_vector_sync
            else "entity sync is disabled"
        )

        # Count the event-entity associations
        event_entity_count = sum(
            len(e.event_associations) for e in events if hasattr(e, "event_associations")
        )
        event_entity_status = (
            f"{event_entity_count} associations"
            if config.enable_event_entity_vector_sync
            else "event-entity association sync is disabled"
        )

        self.logger.info(
            f"Vector store sync finished: {len(events)} events, {entity_status}, {event_entity_status}"
        )

    async def __aenter__(self):
        """Async context manager entry"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - makes sure the resources are released"""
        if self._es_client is not None:
            try:
                await self._es_client.client.close()
                self._es_client = None
                self._event_repo = None
                self._entity_repo = None
            except Exception as e:
                self.logger.warning(f"Failed to close the ES client: {e}")
        return False

    async def _collect_unique_entities(self, events: List[SourceEvent]) -> Dict[str, Entity]:
        """Collect every unique entity"""
        unique_entities = {}

        for event in events:
            if hasattr(event, "event_associations") and event.event_associations:
                for assoc in event.event_associations:
                    entity_id = assoc.entity_id
                    if entity_id not in unique_entities:
                        entity = await self._load_entity_by_id(entity_id)
                        if entity:
                            unique_entities[entity_id] = entity

        return unique_entities

    async def _load_entity_by_id(self, entity_id: str) -> Entity:
        """Load the entities from the database"""
        async with self.session_factory() as session:
            result = await session.execute(select(Entity).where(Entity.id == entity_id))
            return result.scalar_one_or_none()

    async def _sync_entities(self, entities: List[Entity], config: ExtractConfig) -> Dict[str, Any]:
        """
        Sync the entities into the vector store (batched)

        Args:
            entities: the entity list
            config: the extraction configuration

        Returns:
            The statistics
        """
        if not entities:
            return {"total": 0, "indexed": 0}

        # Filter out entity types that do not need indexing
        original_count = len(entities)
        # entities = [e for e in entities if should_index_entity_to_vector_store(e.type)]

        if len(entities) < original_count:
            skipped = original_count - len(entities)
            self.logger.info(f"Filtered out {skipped} entities that do not need indexing")

        if not entities:
            self.logger.info("Every entity was filtered out, skipping the vector sync")
            return {"total": 0, "indexed": 0}

        return await self._batch_sync_entities(entities, config)

    async def _batch_sync_entities(
        self, entities: List[Entity], config: ExtractConfig
    ) -> Dict[str, Any]:
        """
        Sync the entities into the vector store in batch (through the batch utility)

        Args:
            entities: the entity list
            config: the extraction configuration

        Returns:
            The statistics
        """
        from alicecore.utils.batch import batch_generate_embeddings, batch_index_to_es

        start_time = time.perf_counter()
        embedding_client = await get_embedding_client(scenario="general")
        es_client = get_es_client()

        # Stage 1: generate the vectors in batch
        def build_document(entity: Entity, vector: List[float]) -> Dict[str, Any]:
            return {
                "id": entity.id,
                "entity_id": entity.id,
                "source_config_id": entity.source_config_id,
                "type": entity.type,
                "name": entity.name,
                "vector": vector,
                "normalized_name": entity.normalized_name or "",
                "description": entity.description or "",
                "created_time": (
                    entity.created_time.isoformat() if entity.created_time else None
                ),
            }

        embedding_result = await batch_generate_embeddings(
            items=entities,
            text_extractor=lambda e: e.name,
            embedding_client=embedding_client,
            batch_size=config.embedding_batch_size,
            on_success=build_document,
        )

        documents = embedding_result["results"]
        embedding_failed = embedding_result["failed"]
        if embedding_failed:
            # Vector generation was already retried inside the client; a failure here is a real failure.
            # Raise **before** writing the index: rather redo the whole batch than leave "content without vectors" behind.
            raise AIError(
                f"Entity vector generation failed for {embedding_failed}/{len(entities)} rows, the index write was aborted"
            )

        # Stage 2: index in batch
        index_result = await batch_index_to_es(
            documents=documents,
            es_client=es_client,
            index_name=self._entity_repo.INDEX_NAME,
            batch_size=config.index_batch_size,
            routing=config.source_config_id,
        )

        indexed = index_result["indexed"]
        es_failed = index_result["failed"]

        total_time = time.perf_counter() - start_time

        stats = {
            "total": len(entities),
            "indexed": indexed,
            "embedding_failed": embedding_failed,
            "es_failed": es_failed,
            "time": f"{total_time:.2f}s",
        }

        if es_failed > 0:
            self.logger.error(f"Entity sync partly failed: {stats}")
            raise StorageError(
                f"Entity index write failed for {es_failed}/{len(entities)} rows; those entities would be "
                "stored without vectors (unsearchable)"
            )
        self.logger.info(f"Entity sync succeeded: {indexed}/{len(entities)} rows in {total_time:.2f}s")

        return stats

    async def _sync_events(
        self, events: List[SourceEvent], config: ExtractConfig
    ) -> Dict[str, Any]:
        """
        Sync the events into the vector store (batched)

        Args:
            events: the event list
            config: the extraction configuration

        Returns:
            The statistics
        """
        if not events:
            return {"total": 0, "indexed": 0}

        return await self._batch_sync_events(events, config)

    async def _batch_sync_events(
        self, events: List[SourceEvent], config: ExtractConfig
    ) -> Dict[str, Any]:
        """
        Sync the events into the vector store in batch (two stages)

        Args:
            events: the event list
            config: the extraction configuration

        Returns:
            The statistics
        """
        start_time = time.perf_counter()

        embedding_client = await get_embedding_client(scenario="general")
        es_client = get_es_client()

        documents = []
        embedding_failed = 0

        # Stage 1: generate the vectors in batch
        for i in range(0, len(events), config.embedding_batch_size):
            batch = events[i : i + config.embedding_batch_size]

            try:
                # Prepare the text (each event needs two vectors, using the embedding-specific length limit)
                title_texts = [event.title for event in batch]
                content_texts = [
                    f"{event.title}\n\n{event.content[:config.embedding_max_length]}"
                    for event in batch
                ]

                # Generate the vectors in batch
                title_vectors = await embedding_client.batch_generate(title_texts)
                content_vectors = await embedding_client.batch_generate(content_texts)

                # Build the documents
                for event, title_vec, content_vec in zip(batch, title_vectors, content_vectors):
                    doc = self._build_event_document(event, title_vec, content_vec)
                    documents.append(doc)

            except Exception as e:
                self.logger.warning(f"Batch vector generation failed, degrading to a retry: {e}")
                for event in batch:
                    try:
                        title_vec = await embedding_client.generate(event.title)
                        content_for_vec = (
                            f"{event.title}\n\n{event.content[:config.embedding_max_length]}"
                        )
                        content_vec = await embedding_client.generate(content_for_vec)
                        doc = self._build_event_document(event, title_vec, content_vec)
                        documents.append(doc)
                    except Exception as retry_e:
                        self.logger.error(f"Single vector generation failed {event.id}: {retry_e}")
                        embedding_failed += 1

        if embedding_failed:
            # The client already retried, and a one-by-one retry happened here too; a remaining failure aborts rather than writing half an index.
            raise AIError(
                f"Event vector generation failed for {embedding_failed}/{len(events)} rows, the index write was aborted"
            )

        # Stage 2: index in batch (through the utility)
        from alicecore.utils.batch import batch_index_to_es

        index_result = await batch_index_to_es(
            documents=documents,
            es_client=es_client,
            index_name=self._event_repo.INDEX_NAME,
            batch_size=config.index_batch_size,
            routing=config.source_config_id,
        )

        indexed = index_result["indexed"]
        es_failed = index_result["failed"]
        total_time = time.perf_counter() - start_time

        stats = {
            "total": len(events),
            "indexed": indexed,
            "embedding_failed": embedding_failed,
            "es_failed": es_failed,
            "time": f"{total_time:.2f}s",
        }

        if es_failed > 0:
            # Event đã nằm trong DB nhưng không vào được index -> search không bao giờ trả nó về.
            # Báo thành công ở đây là tạo ra tri thức vô hình.
            self.logger.error(f"Event sync partly failed: {stats}")
            raise StorageError(
                f"Event index write failed for {es_failed}/{len(events)} rows; those events would be "
                "stored without vectors (unsearchable)"
            )
        self.logger.info(f"Event sync succeeded: {indexed}/{len(events)} rows in {total_time:.2f}s")

        return stats

    async def _sync_event_entities(
        self, events: List[SourceEvent], config: ExtractConfig
    ) -> Dict[str, Any]:
        """
        Sync the event-entity associations into the vector store (batched)

        Args:
            events: the event list
            config: the extraction configuration

        Returns:
            The statistics
        """
        if not events:
            return {"total": 0, "indexed": 0}

        start_time = time.perf_counter()

        embedding_client = await get_embedding_client(scenario="general")
        es_client = get_es_client()

        # Collect every EventEntity association
        event_entities = self._collect_event_entities(events)

        if not event_entities:
            self.logger.info("There is no event-entity association to sync")
            return {"total": 0, "indexed": 0}

        # Stage 1: generate the vectors in batch (through the utility)
        from alicecore.utils.batch import batch_generate_embeddings, batch_index_to_es

        def build_document(assoc: EventEntity, vector: List[float]) -> Dict[str, Any]:
            return {
                "id": assoc.id,
                "event_id": assoc.event_id,
                "entity_id": assoc.entity_id,
                "source_config_id": config.source_config_id,
                "description": assoc.description or "",
                "vector": vector,
                "created_time": (
                    assoc.created_time.isoformat() if assoc.created_time else None
                ),
                "is_delete": False,
            }

        embedding_result = await batch_generate_embeddings(
            items=event_entities,
            text_extractor=lambda a: a.description or f"{a.event_id}-{a.entity_id}",
            embedding_client=embedding_client,
            batch_size=config.embedding_batch_size,
            on_success=build_document,
        )

        documents = embedding_result["results"]
        embedding_failed = embedding_result["failed"]
        if embedding_failed:
            raise AIError(
                f"Event-entity association vector generation failed for {embedding_failed}/{len(event_entities)} rows, the index write was aborted"
            )

        # Stage 2: index in batch (through the utility)
        index_result = await batch_index_to_es(
            documents=documents,
            es_client=es_client,
            index_name="event_entity_vectors",
            batch_size=config.index_batch_size,
            routing=config.source_config_id,
        )

        indexed = index_result["indexed"]
        es_failed = index_result["failed"]
        total_time = time.perf_counter() - start_time

        stats = {
            "total": len(event_entities),
            "indexed": indexed,
            "embedding_failed": embedding_failed,
            "es_failed": es_failed,
            "time": f"{total_time:.2f}s",
        }

        if es_failed > 0 or embedding_failed > 0:
            self.logger.warning(f"Event-entity association sync partly failed: {stats}")
        else:
            self.logger.info(
                f"Event-entity association sync succeeded: {indexed}/{len(event_entities)} rows in {total_time:.2f}s"
            )

        return stats

    def _build_event_document(
        self, event: SourceEvent, title_vec: List[float], content_vec: List[float]
    ) -> Dict[str, Any]:
        """Build the event document (for vector store indexing)"""
        # Extract the related entity IDs
        entity_ids = []
        if hasattr(event, "event_associations") and event.event_associations:
            entity_ids = [assoc.entity_id for assoc in event.event_associations]

        # Prepare the extra fields
        extra_fields = {}
        if event.extra_data and "tags" in event.extra_data:
            extra_fields["tags"] = event.extra_data["tags"]
        if event.category:
            extra_fields["category"] = event.category
        if event.keywords:
            extra_fields["keywords"] = event.keywords

        return {
            "id": event.id,
            "event_id": event.id,
            "source_config_id": event.source_config_id,
            "source_type": event.source_type,
            "source_id": event.source_id,
            "title": event.title,
            "summary": event.summary or "",
            "content": event.content,
            "title_vector": title_vec,
            "content_vector": content_vec,
            "entity_ids": entity_ids,
            "start_time": event.start_time.isoformat() if event.start_time else None,
            "end_time": event.end_time.isoformat() if event.end_time else None,
            "created_time": (event.created_time.isoformat() if event.created_time else None),
            **extra_fields,
        }
