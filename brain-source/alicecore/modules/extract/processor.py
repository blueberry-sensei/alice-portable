"""
Event processor - LLM extraction

Responsibilities:
- build the prompt and the input (read from YAML)
- call the LLM (with retries)
- recall historical events (as background)
- validate the output
"""

import copy
import json
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select

from alicecore.core.ai.base import BaseLLMClient
from alicecore.core.ai.models import LLMMessage, LLMRole
from alicecore.core.prompt.manager import PromptManager
from alicecore.core.storage.client import get_es_client
from alicecore.core.storage.repositories.event_repository import EventVectorRepository
from alicecore.db import get_session_factory
from alicecore.db.models import Article, EntityType as DBEntityType, SourceEvent
from alicecore.exceptions import ExtractError
from alicecore.modules.extract.config import ExtractConfig
from alicecore.utils import get_logger

logger = get_logger("extract.processor")


class EventProcessor:
    """Event processor - owns the LLM call"""

    def __init__(
        self,
        llm_client: BaseLLMClient,
        prompt_manager: PromptManager,
        config: ExtractConfig,
    ):
        """
        Initialise the event processor

        Args:
            llm_client: the LLM client
            prompt_manager: the prompt manager
            config: the extraction configuration
        """
        self.llm_client = llm_client
        self.prompt_manager = prompt_manager
        self.config = config
        self.session_factory = get_session_factory()

        # State
        self.entity_types: List[DBEntityType] = []

        # Historical event recall (initialised lazily)
        self._es_client = None
        self._event_repo = None
        self._embedding_client = None

    async def initialize(self, entity_types: List[DBEntityType]):
        """
        Initialise (set the entity types)

        Args:
            entity_types: the entity type list
        """
        self.entity_types = entity_types

    async def process(
        self,
        items: List,
        metadata: Dict,
        source_type: str,
    ) -> Dict:
        """
        Run the extraction (calls the LLM)

        Flow:
        1. recall historical events (as background)
        2. build the system prompt
        3. build the input JSON
        4. call the LLM
        5. validate the output

        Args:
            items: a list of ArticleSection or ChatMessage
            metadata: metadata {document_title, chunk_title, previous_context}
            source_type: "ARTICLE" or "CHAT"

        Returns:
            The raw result dictionary the LLM returned
        """
        if not items:
            logger.info("items is empty, skipping extraction")
            return {"type": "response", "data": {"items": [], "meta": {}}}

        try:
            # 1. Recall the historical events
            related_events = await self._recall_related_events(items, source_type)

            # 2. Build the system prompt
            system_prompt = self._build_system_prompt()
            logger.info(f"System prompt length: {len(system_prompt)} characters")

            # 3. Build the input JSON
            user_input = self._build_input(items, metadata, source_type, related_events)
            logger.info(
                f"Input: {len(items)} items, type={source_type}, historical events={len(related_events)}"
            )

            # 4. Build the messages (few-shot form: system + example user + example assistant + real user)
            messages = self._build_messages(system_prompt, user_input)

            # 5. Call the LLM
            schema = self._build_schema()
            result = await self._call_llm_with_retry(messages, schema)

            # 6. Validate the output
            self._validate_output(result)

            # Log the metadata (meta lives inside data)
            meta = result.get("data", {}).get("meta", {})
            logger.info(
                f"LLM returned: reason={meta.get('reason', '')}, "
                f"confidence={meta.get('confidence', 0)}"
            )

            return result

        except Exception as e:
            logger.error(f"Extraction failed: {e}", exc_info=True)
            raise ExtractError(f"Extraction failed: {e}") from e

    async def get_source_created_time(self, items: List, source_type: str) -> Optional[datetime]:
        """
        Get the source creation time

        Args:
            items: the input items
            source_type: "ARTICLE"

        Returns:
            The creation time, or None when there is none
        """
        if source_type != "ARTICLE" or not items:
            return None

        article_id = getattr(items[0], "article_id", None)
        if not article_id:
            return None

        try:
            async with self.session_factory() as session:
                result = await session.execute(
                    select(Article.created_time).where(Article.id == article_id)
                )
                return result.scalar_one_or_none()
        except Exception as e:
            logger.info(f"Failed to read the article creation time: {e}")
            return None

    def _build_system_prompt(self) -> str:
        """Build the system prompt (read from YAML, examples excluded)"""
        tz = ZoneInfo(self.config.timezone)
        time_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M")

        config = self.prompt_manager.get_template_config("extract", test_mode=self.config.test_mode)
        template = config.get("template", "")
        strict_requirements_template = config.get("strict_requirements", "")

        custom_background = self._format_custom(self.config.custom_background)

        # When strict filtering is on, read the rules from YAML and append them
        custom_requirements = self.config.custom_requirements
        if self.config.enable_strict_filtering and strict_requirements_template:
            formatted_strict = self._format_custom(strict_requirements_template)
            if custom_requirements:
                custom_requirements = custom_requirements + "\n" + formatted_strict
            else:
                custom_requirements = formatted_strict

        custom_requirements = self._format_custom(custom_requirements)

        try:
            return template.format(
                time=time_str,
                timezone=self.config.timezone,
                custom_background=custom_background,
                custom_requirements=custom_requirements,
            )
        except KeyError:
            return self.prompt_manager.render(
                "extract",
                time=time_str,
                timezone=self.config.timezone,
                custom_background=custom_background,
                custom_requirements=custom_requirements,
            )

    def _format_custom(self, text: str) -> str:
        """Format the custom text"""
        if not text:
            return ""
        lines = text.strip().split("\n")
        return "\n" + "\n".join(f"    {line}" for line in lines)

    def _build_messages(self, system_prompt: str, user_input: Dict) -> List[LLMMessage]:
        """
        Build the message list (few-shot form)

        Message structure:
        1. system: the system prompt (examples excluded)
        2. user: the example input JSON
        3. assistant: the example output JSON
        4. user: the real input JSON
        """
        # Get the example
        config = self.prompt_manager.get_template_config("extract", test_mode=self.config.test_mode)
        examples = config.get("examples", {})
        example_input = examples.get("input", "")
        example_output = examples.get("output", "")

        messages = [
            # 1. System prompt
            LLMMessage(role=LLMRole.SYSTEM, content=system_prompt),
        ]

        # 2-3. Few-shot example (when present)
        if example_input and example_output:
            messages.append(LLMMessage(role=LLMRole.USER, content=example_input.strip()))
            messages.append(LLMMessage(role=LLMRole.ASSISTANT, content=example_output.strip()))
            logger.debug("Added the few-shot example messages")

        # 4. The real input
        messages.append(
            LLMMessage(role=LLMRole.USER, content=json.dumps(user_input, ensure_ascii=False))
        )

        logger.info(f"Built the message list: {len(messages)} messages")
        return messages

    def _build_input(
        self,
        items: List,
        metadata: Dict,
        source_type: str,
        related_events: List[Dict],
    ) -> Dict:
        """
        Build the input JSON (new shape: data holds only items, meta holds every piece of metadata)

        Shape:
        - type: "request"
        - data: { items: [...] }
        - meta: { source_type, source_title, source_summary, previous_context, entity_types, related_events }
        """
        is_article = source_type == "ARTICLE"

        # Build the items array
        items_data = []
        for i, item in enumerate(items, 1):
            item_data = {"id": i, "content": item.content}
            items_data.append(item_data)

        # Build the entity_types object array (type and description)
        entity_types_data = []
        for et in self.entity_types:
            entity_types_data.append(
                {"type": et.type, "description": et.description or f"{et.name} entity"}
            )

        # Build the input structure (meta lives inside data)
        input_meta = {
            "source_type": "article" if is_article else "chat",
            "source_title": metadata.get("document_title", ""),
            "source_summary": metadata.get("document_summary", ""),
            "entity_types": entity_types_data,
        }

        # Optional field: previous_context
        previous_context = metadata.get("previous_context", "")
        if previous_context:
            input_meta["previous_context"] = previous_context

        # Optional field: related_events
        if related_events:
            input_meta["related_events"] = related_events

        return {
            "type": "request",
            "data": {
                "items": items_data,
                "meta": input_meta,
            },
        }

    def _build_schema(self) -> Dict:
        """Build the output schema"""
        config = self.prompt_manager.get_template_config("extract", test_mode=self.config.test_mode)
        output_schema = config.get("output_schema", {})
        definitions = config.get("definitions", {})

        schema = copy.deepcopy({**output_schema, "definitions": definitions})

        # Entity types are handed to the LLM as prompt guidance (meta.entity_types) but are **not** injected as a strict
        # JSON schema enum: with an open vocabulary the LLM occasionally returns a type outside the set (such as 'action'), and a strict enum would make the whole structured
        # output validation fail and retry to the limit, losing the whole chunk. The parser now soft-filters out-of-set entities one by one instead (without interrupting).
        return schema

    async def _call_llm_with_retry(self, messages: List[LLMMessage], schema: Dict) -> Dict:
        """Call the LLM (with retries)"""
        try:
            logger.info("Calling the LLM (with the retry mechanism)")

            result = await self.llm_client.chat_with_schema(
                messages, response_schema=schema
            )

            logger.info("The LLM call succeeded")
            return result

        except Exception as e:
            logger.error(f"The LLM call failed: {e}")
            raise ExtractError(f"The LLM call failed: {e}") from e

    def _validate_output(self, result: Dict):
        """
        Validate the output format (enhanced validation)

        Strict validation: a structural error raises
        Lenient validation: a content quality problem is only logged as a warning and never interrupts the task
        """
        # === Strict validation: structural errors ===
        if result.get("type") != "response":
            raise ValueError(f"The output type must be 'response', got: {result.get('type')}")

        if "data" not in result:
            raise ValueError("The output is missing the 'data' field")

        if "items" not in result.get("data", {}):
            raise ValueError("The output data is missing the 'items' field")

        if "meta" not in result.get("data", {}):
            raise ValueError("The output data is missing the 'meta' field")

        # === Lenient validation: content quality (warn, never interrupt) ===
        items = result.get("data", {}).get("items", [])
        valid_types = {et.type for et in self.entity_types}

        empty_refs_count = 0
        empty_title_count = 0
        empty_content_count = 0
        invalid_entity_types = set()

        def validate_item(item: Dict, path: str = ""):
            """Validate an event recursively (children included)"""
            nonlocal empty_refs_count, empty_title_count, empty_content_count

            item_path = f"{path}.{item.get('title', '?')}" if path else item.get("title", "?")

            # Validate references (counted only, not logged individually)
            refs = item.get("references", [])
            if not refs or len(refs) == 0:
                empty_refs_count += 1

            # Validate title (counted only, not logged individually)
            if not item.get("title", "").strip():
                empty_title_count += 1

            # Validate content (counted only, not logged individually)
            if not item.get("content", "").strip():
                empty_content_count += 1

            # Validate the entity types
            entities = item.get("entities", [])
            for entity in entities:
                entity_type = entity.get("type")
                if entity_type and entity_type not in valid_types:
                    invalid_entity_types.add(entity_type)

            # Validate the children recursively
            children = item.get("children", [])
            for child in children:
                validate_item(child, item_path)

        # Validate every event
        for item in items:
            validate_item(item)

        # Summarise the warnings
        if empty_refs_count > 0:
            logger.warning(f"Output validation: {empty_refs_count} events have empty references")
        if empty_title_count > 0:
            logger.warning(f"Output validation: {empty_title_count} events have an empty title")
        if empty_content_count > 0:
            logger.warning(f"Output validation: {empty_content_count} events have empty content")
        if invalid_entity_types:
            logger.warning(
                f"Output validation: found invalid entity types {invalid_entity_types}, "
                f"allowed types: {sorted(valid_types)}"
            )

    async def _recall_related_events(self, items: List, _source_type: str = None) -> List[Dict]:
        """
        Recall historical events (as a reference for categorisation and entity naming)

        Args:
            items: a list of ArticleSection or ChatMessage
            _source_type: reserved for a future extension

        Returns:
            The historical event list [{title, category, entities: [{type, name}]}]
        """
        if not self.config.enable_related_events:
            return []

        try:
            await self._ensure_recall_deps()

            # Build the query text
            content_text = " ".join([item.content for item in items])

            # Generate the vector (using the embedding-specific length limit, so the model's token cap is not exceeded)
            max_len = self.config.embedding_max_length
            content_vector = await self._embedding_client.generate_embedding(content_text[:max_len])

            # Recall from the vector store
            results = await self._event_repo.search_similar_by_content(
                query_vector=content_vector,
                k=self.config.related_events_top_k,
                source_config_id=self.config.source_config_id,
            )

            # Filter out low-similarity results
            results = [
                r for r in results if r.get("_score", 0) >= self.config.related_events_threshold
            ]

            if not results:
                return []

            # Load the event details from the database
            event_ids = [r["event_id"] for r in results]
            related_events = []

            async with self.session_factory() as session:
                from alicecore.db.models import EventEntity
                from sqlalchemy.orm import selectinload

                stmt = (
                    select(SourceEvent)
                    .options(
                        selectinload(SourceEvent.event_associations).selectinload(
                            EventEntity.entity
                        )
                    )
                    .where(SourceEvent.id.in_(event_ids))
                )
                db_events = (await session.execute(stmt)).scalars().all()

                for event in db_events:
                    entities = [
                        {"type": ee.entity.type, "name": ee.entity.name}
                        for ee in event.event_associations
                        if ee.entity
                    ]
                    related_events.append(
                        {
                            "title": event.title,
                            "category": event.category or "",
                            "entities": entities[:10],
                        }
                    )

            logger.info(f"Recalled {len(related_events)} related historical events as a reference")
            return related_events

        except Exception as e:
            logger.info(f"Historical event recall failed: {e}")
            return []

    async def _ensure_recall_deps(self):
        """Make sure the recall dependencies are available"""
        if self._event_repo is None:
            from alicecore.modules.load.processor import DocumentProcessor

            self._es_client = get_es_client()
            self._event_repo = EventVectorRepository(self._es_client)
            self._embedding_client = DocumentProcessor(llm_client=self.llm_client)
