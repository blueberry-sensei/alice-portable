"""
Parser module - the complete parsing layer

Responsibilities:
- event parsing: the LLM result -> SourceEvent
- entity parsing: create or find an Entity
- relation parsing: create the EventEntity association
- value type inference: parse the entity value type
"""

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from alicecore.db import Entity, EventEntity, SourceEvent, get_session_factory
from alicecore.db.models import EntityType as DBEntityType
from alicecore.modules.extract.config import ExtractConfig
from alicecore.utils import get_logger, get_utc_now

logger = get_logger("extract.parser")


@dataclass
class ParseContext:
    """Parsing context"""

    source_config_id: str
    source_type: str  # "ARTICLE" or "CHAT"
    source_id: str
    chunk_id: str
    source_created_time: Optional[datetime] = None


class ResultParser:
    """
    Result parser - coordinates the whole parsing flow

    Flow:
    1. parse the events: the dictionary the LLM returned -> a SourceEvent list
    2. parse the entities: create or find an Entity and build the EventEntity association
    """

    def __init__(self, config: ExtractConfig):
        """
        Initialise the parser

        Args:
            config: the extraction configuration
        """
        self.config = config
        self.session_factory = get_session_factory()
        self.value_parser = EntityValueParser()

    def parse_events(
        self,
        raw_items: List[Dict],
        items: List,
        context: ParseContext,
    ) -> List[SourceEvent]:
        """
        Parse the events (the LLM result -> a SourceEvent list)

        Args:
            raw_items: the event data list the LLM returned
            items: the raw input items (used to resolve references)
            context: the parsing context

        Returns:
            The SourceEvent list (flattened, every level included)
        """
        # Build the index -> item map (1-based)
        index_map = {i + 1: item for i, item in enumerate(items)}
        all_item_ids = [item.id for item in items]

        # Parse recursively (stored flattened)
        all_events = []
        for item_data in raw_items:
            events = self._parse_item_recursive(
                item_data,
                index_map,
                all_item_ids,
                context,
                parent_id=None,
                level=0,
            )
            all_events.extend(events)

        return all_events

    def _parse_item_recursive(
        self,
        item_data: Dict,
        index_map: Dict,
        all_item_ids: List[str],
        context: ParseContext,
        parent_id: Optional[str],
        level: int = 0,
    ) -> List[SourceEvent]:
        """
        Parse one event recursively (returns a flat list)

        Args:
            item_data: the event data
            index_map: the {ordinal: item} map
            all_item_ids: the UUID list of every item
            context: the parsing context
            parent_id: the parent event ID
            level: hierarchy depth (0=L0 top level, 1=L1, 2=L2)

        Returns:
            [parent, child1, child2, ...] as a flat list
        """
        # Filter out invalid events (as marked by the LLM)
        if not item_data.get("is_valid", True):
            reason = item_data.get("reason", "")
            logger.info(f"Filtered out an invalid event: {item_data.get('title', '')} - {reason}")
            return []

        # Parse the references
        ref_indices = item_data.get("references", [])
        valid_refs = self._parse_references(ref_indices, index_map, all_item_ids)

        # Extract the time
        start_time, end_time = self._extract_times(
            context.source_type, valid_refs, index_map, context.source_created_time
        )

        # Convert the entity format
        raw_entities = self._parse_raw_entities(item_data.get("entities", []))

        # Extract the fields (an empty string becomes None)
        category = item_data.get("category") or None
        priority = item_data.get("priority") or None
        status = item_data.get("status") or None
        keywords = item_data.get("keywords") or None

        # Create the event
        event_id = str(uuid.uuid4())
        event = SourceEvent(
            id=event_id,
            source_config_id=context.source_config_id,
            source_type=context.source_type,
            type=context.source_type,
            source_id=context.source_id or "",
            article_id=context.source_id if context.source_type == "ARTICLE" else None,
            chunk_id=context.chunk_id,
            parent_id=parent_id,
            rank=0,  # rank is assigned centrally in the extractor
            level=level,
            title=item_data.get("title", ""),
            summary=item_data.get("summary", ""),
            content=item_data.get("content", ""),
            category=category,
            keywords=keywords,
            priority=priority,
            status=status,
            start_time=start_time or get_utc_now().replace(tzinfo=None),
            end_time=end_time,
            references=valid_refs,
            extra_data={
                "raw_entities": {"entities": raw_entities},
                "raw_data": item_data,  # the raw data is kept for quality filtering
            },
        )

        result = [event]

        # Handle the child events recursively
        children = item_data.get("children", [])
        if children:
            logger.info(f"Event '{event.title}' has {len(children)} child events")
            for child_data in children:
                child_events = self._parse_item_recursive(
                    child_data,
                    index_map,
                    all_item_ids,
                    context,
                    parent_id=event_id,
                    level=level + 1,
                )
                result.extend(child_events)

        return result

    def _parse_references(
        self,
        ref_indices: List,
        index_map: Dict,
        all_item_ids: List[str],
    ) -> List[str]:
        """
        Parse the references (index -> UUID)

        Args:
            ref_indices: the reference ordinals the LLM returned
            index_map: the {ordinal: item} map
            all_item_ids: the UUID list of every item (used as a fallback)

        Returns:
            The UUID list
        """
        valid_refs = []
        invalid_indices = []

        for idx in ref_indices:
            # Accept a numeric string too
            if isinstance(idx, str):
                if idx.isdigit():
                    idx = int(idx)
                else:
                    invalid_indices.append((idx, "not a numeric string"))
                    continue

            if isinstance(idx, int) and idx in index_map:
                valid_refs.append(index_map[idx].id)
            elif isinstance(idx, int):
                invalid_indices.append((idx, f"out of range (valid range 1-{len(index_map)})"))

            # Log the invalid indices
        if invalid_indices:
            logger.warning(f"Contains invalid references: {invalid_indices}")

        # Fallback: use every item when no reference is valid
        if not valid_refs:
            if ref_indices:
                logger.warning(f"Every reference is invalid ({len(ref_indices)} in total), falling back to every section")
            else:
                logger.warning("The references are empty, falling back to every section")
            valid_refs = all_item_ids

        return valid_refs

    def _extract_times(
        self,
        source_type: str,
        valid_refs: List[str],
        index_map: Dict,
        source_created_time: Optional[datetime],
    ) -> tuple:
        """
        Extract the time

        - ARTICLE: use the document creation time

        Args:
            source_type: "ARTICLE"
            valid_refs: the valid reference UUID list
            index_map: the {ordinal: item} map
            source_created_time: the source creation time

        Returns:
            (start_time, end_time)
        """
        if source_type == "ARTICLE":
            if source_created_time:
                return source_created_time, source_created_time
            return None, None

        return None, None

    def _parse_raw_entities(self, entities_list: List) -> List[Dict]:
        """
        Parse the entity format (normalised to a list)

        Args:
            entities_list: the entity list

        Returns:
            The normalised entity list
        """
        result = []

        if isinstance(entities_list, list):
            for entity in entities_list:
                if isinstance(entity, dict):
                    result.append(
                        {
                            "type": entity.get("type", ""),
                            "name": entity.get("name", ""),
                            "description": entity.get("description", ""),
                        }
                    )
        return result

    async def process_entity_associations(
        self,
        events: List[SourceEvent],
        entity_types: List[DBEntityType],
    ) -> List[SourceEvent]:
        """
        Handle the entity associations (the full flow)

        Flow:
        1. deduplicate and create the entities (a cache avoids repeated queries)
        2. merge the descriptions (several descriptions of one entity are merged)
        3. create the EventEntity associations

        Args:
            events: the event list (extra_data["raw_entities"]["entities"] holds the entity data)
            entity_types: the entity type list

        Returns:
            The processed event list (event_associations set)
        """
        if not events:
            return events

        entity_cache = {}

        for event in events:
            raw_entities = event.extra_data.get("raw_entities", {}).get("entities", [])
            if not raw_entities:
                event.event_associations = []
                continue

            entity_map = {}

            # Collect the entities (deduplicate and create)
            for entity_data in raw_entities:
                cache_key = self._build_cache_key(entity_data)

                if cache_key in entity_cache:
                    entity = entity_cache[cache_key]
                else:
                    entity = await self._get_or_create_entity(entity_data, entity_types)
                    if entity:
                        entity_cache[cache_key] = entity

                if entity is None:
                    continue

                # Collect the descriptions (deduplicated)
                if entity.id not in entity_map:
                    entity_map[entity.id] = {"name": entity.name, "descriptions": []}

                description = entity_data.get("description", "").strip()
                if description and description not in entity_map[entity.id]["descriptions"]:
                    entity_map[entity.id]["descriptions"].append(description)

            # Create the EventEntity associations
            event.event_associations = []
            for entity_id, info in entity_map.items():
                final_description = self._merge_descriptions(info["descriptions"])
                assoc = EventEntity(
                    id=str(uuid.uuid4()),
                    event_id=event.id,
                    entity_id=entity_id,
                    description=final_description,
                )
                event.event_associations.append(assoc)

        return events

    def _build_cache_key(self, entity_data: Dict) -> tuple:
        """Build the entity cache key"""
        return (
            entity_data.get("type", ""),
            entity_data.get("name", "").strip().lower(),
        )

    def _merge_descriptions(self, descriptions: List[str]) -> str:
        """Merge the description list"""
        return "\u3001".join(descriptions) if descriptions else ""

    async def _get_or_create_entity(
        self,
        entity_data: Dict,
        entity_types: List[DBEntityType],
    ) -> Optional[Entity]:
        """
        Find or create an entity (concurrency safe)

        A retry mechanism handles lock wait timeouts and deadlocks

        Args:
            entity_data: the entity data dictionary {type, name, description, ...}
            entity_types: the entity type list

        Returns:
            The Entity object, or None when the entity type or the name is invalid
        """
        entity_name = entity_data.get("name", "").strip()
        if len(entity_name) <= 1:
            return None

        normalized_name = entity_name.lower()
        max_retries = 3
        base_delay = 0.1

        for attempt in range(max_retries):
            try:
                return await self._get_or_create_entity_inner(
                    entity_data, normalized_name, entity_types
                )
            except OperationalError as e:
                error_str = str(e)
                is_lock_timeout = "1205" in error_str or "Lock wait timeout" in error_str
                is_deadlock = "1213" in error_str or "Deadlock" in error_str
                is_lost_connection = "2013" in error_str or "Lost connection" in error_str

                if (is_lock_timeout or is_deadlock or is_lost_connection) and attempt < max_retries - 1:
                    delay = base_delay * (2**attempt)
                    logger.warning(f"Entity creation raised ({e.orig if hasattr(e, 'orig') else e}), retry {attempt + 1}/{max_retries}: {delay}s")
                    await asyncio.sleep(delay)
                    continue
                raise

        return None

    async def _get_or_create_entity_inner(
        self,
        entity_data: Dict,
        normalized_name: str,
        entity_types: List[DBEntityType],
    ) -> Optional[Entity]:
        """Perform the actual find-or-create"""
        async with self.session_factory() as session:
            # Look for an existing one
            existing = await session.execute(
                select(Entity)
                .where(Entity.source_config_id == self.config.source_config_id)
                .where(Entity.type == entity_data["type"])
                .where(Entity.normalized_name == normalized_name)
            )
            entity = existing.scalar_one_or_none()
            if entity:
                return entity

            # Look up the entity type
            entity_type = next((et for et in entity_types if et.type == entity_data["type"]), None)
            if not entity_type:
                logger.warning(
                    f"Skipping an invalid entity type: type={entity_data['type']}, "
                    f"name={entity_data.get('name', 'N/A')}"
                )
                return None

            # Create a new entity
            entity = Entity(
                id=str(uuid.uuid4()),
                source_config_id=self.config.source_config_id,
                entity_type_id=entity_type.id,
                type=entity_data["type"],
                name=entity_data["name"],
                normalized_name=normalized_name,
                description=None,
            )

            # Parse the typed value
            typed_fields = self._parse_entity_value(entity_data, entity_type)
            if typed_fields:
                source = typed_fields.pop("_source", "code")
                entity.value_type = typed_fields.get("value_type")
                entity.value_raw = typed_fields.get("value_raw")
                entity.int_value = typed_fields.get("int_value")
                entity.float_value = typed_fields.get("float_value")
                entity.datetime_value = typed_fields.get("datetime_value")
                entity.bool_value = typed_fields.get("bool_value")
                entity.enum_value = typed_fields.get("enum_value")
                entity.value_unit = typed_fields.get("value_unit")
                entity.value_confidence = typed_fields.get("value_confidence")

                # Log the parse result of a non-text type
                if entity.value_type and entity.value_type != "text":
                    unit_str = f", unit={entity.value_unit}" if entity.value_unit else ""
                    logger.debug(
                        f"Entity value parsed: {entity_data['name'][:15]} -> "
                        f"{entity.value_type}({source}){unit_str}"
                    )

            try:
                session.add(entity)
                await session.commit()
                await session.refresh(entity)
                return entity
            except IntegrityError:
                # Concurrency conflict: query again
                await session.rollback()
                logger.debug(f"Entity concurrency conflict, querying again: {entity_data['name']}")

                retry = await session.execute(
                    select(Entity)
                    .where(Entity.source_config_id == self.config.source_config_id)
                    .where(Entity.type == entity_data["type"])
                    .where(Entity.normalized_name == normalized_name)
                )
                return retry.scalar_one_or_none()

    def _parse_entity_value(self, entity_data: Dict, entity_type: DBEntityType) -> Dict[str, Any]:
        """
        Parse the entity value type (the LLM first, code as a fallback)

        Priority:
        1. the value_type the LLM returned (when valid)
        2. the code fallback (through EntityValueParser)

        Args:
            entity_data: the entity data dictionary, possibly holding value_type, value, unit
            entity_type: the entity type object, holding value_constraints

        Returns:
            The typed field dictionary, holding value_type, value_raw, int_value and so on
        """
        name = entity_data["name"]
        llm_type = entity_data.get("value_type")
        llm_value = entity_data.get("value")
        llm_unit = entity_data.get("unit")

        valid_types = ("text", "int", "float", "datetime", "bool", "enum")
        value_constraints = getattr(entity_type, "value_constraints", None)

        # The LLM value is valid -> use the type and value it returned
        if llm_type and llm_type in valid_types:
            value_raw = self._build_value_raw(name, llm_value, llm_unit, llm_type)

            fields = {
                "value_type": llm_type,
                "value_raw": value_raw,
                "int_value": None,
                "float_value": None,
                "datetime_value": None,
                "bool_value": None,
                "enum_value": None,
                "value_unit": llm_unit,
                "value_confidence": Decimal("0.90"),
            }

            # Parse the value the LLM returned with EntityValueParser
            if llm_value:
                try:
                    parsed = self.value_parser.parse(
                        llm_value,
                        entity_type=entity_data["type"],
                        value_constraints=value_constraints,
                    )
                    if parsed:
                        if parsed["type"] == "int":
                            fields["int_value"] = parsed["value"]
                        elif parsed["type"] == "float":
                            fields["float_value"] = Decimal(str(parsed["value"]))
                        elif parsed["type"] == "datetime":
                            fields["datetime_value"] = parsed["value"]
                        elif parsed["type"] == "bool":
                            fields["bool_value"] = parsed["value"]
                        elif parsed["type"] == "enum":
                            fields["enum_value"] = parsed["value"]
                        if parsed.get("unit") and not llm_unit:
                            fields["value_unit"] = parsed["unit"]
                except Exception as e:
                    logger.debug(f"LLM value parsing failed: {llm_value}, error={e}")

            fields["_source"] = "llm"
            return fields

        # Code fallback: parse from name with EntityValueParser
        result = self.value_parser.parse_to_typed_fields(
            name, entity_type=entity_data["type"], value_constraints=value_constraints
        )
        result["_source"] = "code"
        return result

    def _build_value_raw(self, name: str, value: str, unit: str, value_type: str) -> str:
        """Build the full value_raw"""
        name = name or ""

        if value_type == "text" or not value:
            return name

        if value_type == "datetime":
            if re.search(r"(\d{4}\u5e74|\d{1,2}\u6708|\d{1,2}\u65e5|\d{1,2}[:\uff1a\u70b9]|\d{4}-)", name):
                return name
            return name + value

        if re.search(r"\d", name):
            if unit and unit not in name:
                return name + unit
            return name

        result = name + value
        if unit and unit not in result:
            result += unit

        return result


class EntityValueParser:
    """
    Entity value type parser

    Purpose: parse entity name text into a typed value
    Supported types: int, float, datetime, bool, enum, text
    """

    # CJK numeral map (escapes on purpose: the source carries no Chinese, but CJK documents still parse)
    CN_NUM_MAP = {
        "\u96f6": 0,
        "\u4e00": 1,
        "\u4e8c": 2,
        "\u4e09": 3,
        "\u56db": 4,
        "\u4e94": 5,
        "\u516d": 6,
        "\u4e03": 7,
        "\u516b": 8,
        "\u4e5d": 9,
        "\u5341": 10,
        "\u767e": 100,
        "\u5343": 1000,
        "\u4e07": 10000,
        "\u4ebf": 100000000,
        "\u5146": 1000000000000,
    }

    # Unit multiplier map (escapes on purpose, same reason as above)
    UNIT_MULTIPLIER = {
        "\u5143": 1,
        "\u7f8e\u5143": 1,
        "USD": 1,
        "$": 1,
        "\u4e07": 10000,
        "\u4e07\u5143": 10000,
        "\u4ebf": 100000000,
        "\u4ebf\u5143": 100000000,
        "\u514b": 0.001,
        "g": 0.001,
        "kg": 1,
        "\u516c\u65a4": 1,
        "\u5343\u514b": 1,
        "\u5428": 1000,
        "\u7c73": 1,
        "m": 1,
        "\u516c\u91cc": 1000,
        "km": 1000,
        "\u5398\u7c73": 0.01,
        "cm": 0.01,
        "\u79d2": 1,
        "s": 1,
        "\u5206\u949f": 60,
        "\u5c0f\u65f6": 3600,
        "\u5929": 86400,
    }

    # Boolean map (escapes on purpose, same reason as above)
    BOOL_TRUE = ["\u662f", "\u5bf9", "\u771f", "yes", "true", "\u5df2", "\u6709", "\u542f\u7528", "\u5f00\u542f"]
    BOOL_FALSE = ["\u5426", "\u9519", "\u5047", "no", "false", "\u672a", "\u65e0", "\u7981\u7528", "\u5173\u95ed"]

    def parse(
        self,
        text: str,
        entity_type: Optional[str] = None,
        entity_type_category: Optional[str] = None,
        value_constraints: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Parse an entity value

        Args:
            text: the raw text
            entity_type: the entity type
            entity_type_category: the attribute type category
            value_constraints: the value constraints

        Returns:
            The parse result dictionary {type, raw, value, unit, confidence}, or None
        """
        if not text or not text.strip():
            return None

        text = text.strip()

        # Strict mode: when value_constraints.type is configured, force that type
        if value_constraints and "type" in value_constraints:
            constraint_type = value_constraints["type"]
            result = None

            if constraint_type == "int":
                result = self._parse_number(text, entity_type, value_constraints, force_int=True)
            elif constraint_type == "float":
                result = self._parse_number(text, entity_type, value_constraints, force_float=True)
            elif constraint_type == "enum":
                result = self._parse_enum(text, entity_type, value_constraints)
            elif constraint_type == "datetime":
                result = self._parse_compact_datetime(text) or self._parse_datetime(
                    text, entity_type, value_constraints
                )
            elif constraint_type == "bool":
                result = self._parse_bool(text, entity_type, value_constraints)
            elif constraint_type == "text":
                result = self._parse_text(text)

            if result:
                result["raw"] = text
            return result

        # Lenient mode: try each type in priority order
        time_keywords = ["time", "date", "\u65f6\u95f4", "\u65e5\u671f", "datetime"]
        is_time_type = (entity_type_category and entity_type_category.lower() in time_keywords) or (
            entity_type
            and any(kw in entity_type.lower() for kw in ["\u65f6\u95f4", "\u65e5\u671f", "time", "date"])
        )

        if is_time_type:
            compact_result = self._parse_compact_datetime(text)
            if compact_result:
                compact_result["raw"] = text
                return compact_result

        parsers = [
            self._parse_datetime,
            self._parse_number,
            self._parse_enum,
            self._parse_bool,
        ]

        for parser in parsers:
            result = parser(text, entity_type, value_constraints)
            if result:
                result["raw"] = text
                return result

        return {"type": "text", "raw": text, "value": text, "unit": None, "confidence": 1.0}

    def _parse_number(
        self,
        text: str,
        _entity_type: Optional[str] = None,
        _value_constraints: Optional[Dict[str, Any]] = None,
        force_int: bool = False,
        force_float: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Parse a numeric type"""
        configured_unit = _value_constraints.get("unit") if _value_constraints else None
        if configured_unit:
            unit_match_result = self._try_parse_with_unit(
                text, configured_unit, force_int, force_float
            )
            if unit_match_result:
                return unit_match_result

        pattern = r"^([\d,.]+(?:e[+-]?\d+)?)\s*([a-zA-Z\u4e00-\u9fa5]*?)$"
        match = re.match(pattern, text, re.IGNORECASE)

        if match:
            number_str = match.group(1).replace(",", "")
            unit = match.group(2).strip() or None

            try:
                if force_int:
                    if "." in number_str or "e" in number_str.lower():
                        return None
                    num = int(number_str)
                    if unit and unit in self.UNIT_MULTIPLIER:
                        num = num * self.UNIT_MULTIPLIER[unit]
                    return {"type": "int", "value": int(num), "unit": unit, "confidence": 0.95}

                if force_float:
                    if "e" in number_str.lower():
                        num = float(number_str)
                    elif "." in number_str:
                        num = float(number_str)
                    else:
                        num = int(number_str)
                    if unit and unit in self.UNIT_MULTIPLIER:
                        num = num * self.UNIT_MULTIPLIER[unit]
                    return {"type": "float", "value": float(num), "unit": unit, "confidence": 0.95}

                if "e" in number_str.lower():
                    num = float(number_str)
                elif "." in number_str:
                    num = float(number_str)
                else:
                    num = int(number_str)

                if unit and unit in self.UNIT_MULTIPLIER:
                    num = num * self.UNIT_MULTIPLIER[unit]

                if isinstance(num, int):
                    value_type = "int"
                    value = num
                elif isinstance(num, float) and num.is_integer():
                    value_type = "int"
                    value = int(num)
                else:
                    value_type = "float"
                    value = float(num)

                return {"type": value_type, "value": value, "unit": unit, "confidence": 0.95}
            except ValueError:
                pass

        cn_result = self._parse_chinese_number(text)
        if cn_result:
            if force_int and cn_result["type"] != "int":
                return None
            if force_float and cn_result["type"] != "float":
                cn_result["type"] = "float"
                cn_result["value"] = float(cn_result["value"])
            return cn_result

        return None

    def _parse_chinese_number(self, text: str) -> Optional[Dict[str, Any]]:
        """Parse a CJK numeral"""
        if len(text) > 6:
            return None

        pattern = r"^([\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u4e07\u4ebf\u5146]+)$"
        match = re.match(pattern, text)

        if not match:
            return None

        cn_text = match.group(1)
        try:
            value = self._simple_chinese_to_num(cn_text)
            if value is not None:
                return {"type": "int", "value": value, "unit": None, "confidence": 0.85}
        except Exception as e:
            logging.debug(f"CJK numeral parsing failed: {text}, error={e}")

        return None

    def _try_parse_with_unit(
        self,
        text: str,
        configured_unit: str,
        force_int: bool = False,
        force_float: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Smart unit matching"""
        quantifiers = ["\u4e2a", "\u4ef6", "\u6761", "\u9879", "\u6279", "\u6b21", "\u7b14", "\u5355", "\u7ec4"]

        for quantifier in quantifiers + [""]:
            pattern = rf"^([\d,.]+(?:e[+-]?\d+)?){quantifier}{re.escape(configured_unit)}$"
            match = re.match(pattern, text, re.IGNORECASE)

            if match:
                number_str = match.group(1).replace(",", "")
                try:
                    if force_int:
                        if "." in number_str or "e" in number_str.lower():
                            continue
                        num = int(number_str)
                        return {
                            "type": "int",
                            "value": num,
                            "unit": configured_unit,
                            "confidence": 0.95,
                        }
                    elif force_float:
                        num = float(number_str)
                        return {
                            "type": "float",
                            "value": num,
                            "unit": configured_unit,
                            "confidence": 0.95,
                        }
                    else:
                        if "." in number_str or "e" in number_str.lower():
                            num = float(number_str)
                            return {
                                "type": "float",
                                "value": num,
                                "unit": configured_unit,
                                "confidence": 0.95,
                            }
                        else:
                            num = int(number_str)
                            return {
                                "type": "int",
                                "value": num,
                                "unit": configured_unit,
                                "confidence": 0.95,
                            }
                except ValueError:
                    continue

            # Try a CJK numeral
            cn_pattern = (
                rf"^([\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u4e07\u4ebf\u5146]+){quantifier}{re.escape(configured_unit)}$"
            )
            cn_match = re.match(cn_pattern, text)

            if cn_match:
                cn_text = cn_match.group(1)
                try:
                    value = self._simple_chinese_to_num(cn_text)
                    if value is not None:
                        if force_float:
                            return {
                                "type": "float",
                                "value": float(value),
                                "unit": configured_unit,
                                "confidence": 0.90,
                            }
                        else:
                            return {
                                "type": "int",
                                "value": value,
                                "unit": configured_unit,
                                "confidence": 0.90,
                            }
                except Exception as e:
                    logging.debug(f"CJK numeral unit matching failed: {text}, error={e}")
                    continue

        return None

    def _simple_chinese_to_num(self, cn_text: str) -> Optional[int]:
        """Simplified CJK numeral conversion"""
        total = 0
        unit = 1

        for char in reversed(cn_text):
            num = self.CN_NUM_MAP.get(char)
            if num is None:
                return None

            if num >= 10:
                if num > unit:
                    unit = num
                else:
                    unit *= num
            else:
                total += num * unit

        return total if total > 0 else None

    def _parse_datetime(
        self,
        text: str,
        _entity_type: Optional[str] = None,
        _value_constraints: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Parse a time type"""
        text_clean = text.replace("\u5e74", "-").replace("\u6708", "-").replace("\u65e5", "")
        text_clean = re.sub(r"-+", "-", text_clean)
        text_clean = text_clean.strip("-")

        # Full date and time
        pattern_datetime = r"(\d{4})-(\d{1,2})-(\d{1,2})[\sT]+(\d{1,2}):(\d{1,2}):(\d{1,2})"
        match = re.search(pattern_datetime, text_clean)
        if match:
            try:
                dt = datetime(
                    int(match.group(1)),
                    int(match.group(2)),
                    int(match.group(3)),
                    int(match.group(4)),
                    int(match.group(5)),
                    int(match.group(6)),
                )
                return {"type": "datetime", "value": dt, "unit": None, "confidence": 0.98}
            except ValueError:
                pass

        # Date and time (to the minute)
        pattern_datetime_min = r"(\d{4})-(\d{1,2})-(\d{1,2})[\sT]+(\d{1,2}):(\d{1,2})"
        match = re.search(pattern_datetime_min, text_clean)
        if match:
            try:
                dt = datetime(
                    int(match.group(1)),
                    int(match.group(2)),
                    int(match.group(3)),
                    int(match.group(4)),
                    int(match.group(5)),
                )
                return {"type": "datetime", "value": dt, "unit": None, "confidence": 0.95}
            except ValueError:
                pass

        # ISO date
        pattern_iso = r"(\d{4})-(\d{1,2})-(\d{1,2})"
        match = re.search(pattern_iso, text_clean)
        if match:
            try:
                dt = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                return {"type": "datetime", "value": dt, "unit": None, "confidence": 0.98}
            except ValueError:
                pass

        # YYYY-MM
        pattern_month = r"(\d{4})-(\d{1,2})(?:[^\d]|$)"
        match = re.search(pattern_month, text_clean)
        if match:
            try:
                dt = datetime(int(match.group(1)), int(match.group(2)), 1)
                return {"type": "datetime", "value": dt, "unit": None, "confidence": 0.90}
            except ValueError:
                pass

        # Year only
        money_patterns = [
            r"\u5143",
            r"\u7f8e\u5143",
            r"USD",
            r"\$",
            r"¥",
            r"\u4e07\u5143",
            r"\u4ebf\u5143",
            r"\u5757",
            r"\u89d2",
            r"\u5206",
            r"\u5757\u94b1",
            r"\u6bdb",
            r"EUR",
            r"£",
            r"RMB",
            r"CNY",
            r"HKD",
            r"/\u6708",
            r"/\u5e74",
            r"/\u5929",
            r"/\u5468",
            r"\u6bcf\u6708",
            r"\u6bcf\u5e74",
            r"\u8d77",
            r"\u5de6\u53f3",
            r"\u7ea6",
        ]
        has_money_indicator = any(
            re.search(pattern, text, re.IGNORECASE) for pattern in money_patterns
        )

        pattern_year = r"^(\d{4})(?:[^\d]|$)"
        match = re.match(pattern_year, text_clean)
        if match and not has_money_indicator:
            year = int(match.group(1))
            if 1970 <= year <= 2099:
                try:
                    dt = datetime(year, 1, 1)
                    return {"type": "datetime", "value": dt, "unit": None, "confidence": 0.85}
                except ValueError:
                    pass

        return None

    def _parse_compact_datetime(self, text: str) -> Optional[Dict[str, Any]]:
        """Parse a compact date format"""
        # YYYYMMDDHHmmss (14 digits)
        if re.match(r"^(\d{14})$", text):
            try:
                year = int(text[0:4])
                month = int(text[4:6])
                day = int(text[6:8])
                hour = int(text[8:10])
                minute = int(text[10:12])
                second = int(text[12:14])
                dt = datetime(year, month, day, hour, minute, second)
                return {"type": "datetime", "value": dt, "unit": None, "confidence": 0.95}
            except ValueError:
                return None

        # YYYYMMDDHHmm (12 digits)
        if re.match(r"^(\d{12})$", text):
            try:
                year = int(text[0:4])
                month = int(text[4:6])
                day = int(text[6:8])
                hour = int(text[8:10])
                minute = int(text[10:12])
                dt = datetime(year, month, day, hour, minute)
                return {"type": "datetime", "value": dt, "unit": None, "confidence": 0.92}
            except ValueError:
                return None

        # YYYYMMDD (8 digits)
        if re.match(r"^(\d{8})$", text):
            try:
                year = int(text[0:4])
                month = int(text[4:6])
                day = int(text[6:8])
                dt = datetime(year, month, day)
                return {"type": "datetime", "value": dt, "unit": None, "confidence": 0.95}
            except ValueError:
                return None

        # YYYYMM (6 digits)
        if re.match(r"^(\d{6})$", text):
            try:
                year = int(text[0:4])
                month = int(text[4:6])
                dt = datetime(year, month, 1)
                return {"type": "datetime", "value": dt, "unit": None, "confidence": 0.90}
            except ValueError:
                return None

        return None

    def _parse_bool(
        self,
        text: str,
        _entity_type: Optional[str] = None,
        _value_constraints: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Parse a boolean type"""
        if len(text) > 10:
            return None

        text_lower = text.lower().strip()

        if text_lower in self.BOOL_TRUE:
            return {"type": "bool", "value": True, "unit": None, "confidence": 0.95}

        if text_lower in self.BOOL_FALSE:
            return {"type": "bool", "value": False, "unit": None, "confidence": 0.95}

        return None

    def _parse_enum(
        self,
        text: str,
        _entity_type: Optional[str] = None,
        value_constraints: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Parse an enum type"""
        if not value_constraints or "enum_values" not in value_constraints:
            return None

        enum_values: List[str] = value_constraints["enum_values"]

        # Exact match
        if text in enum_values:
            return {"type": "enum", "value": text, "unit": None, "confidence": 1.0}

        # Fuzzy match
        text_lower = text.lower()
        for enum_val in enum_values:
            if enum_val.lower() in text_lower or text_lower in enum_val.lower():
                return {"type": "enum", "value": enum_val, "unit": None, "confidence": 0.80}

        return {"type": "enum", "value": "UNKNOWN", "unit": None, "confidence": 0.0}

    def _parse_text(self, text: str) -> Optional[Dict[str, Any]]:
        """Parse as a plain text type"""
        return {"type": "text", "value": text, "unit": None, "confidence": 1.0}

    def parse_to_typed_fields(
        self,
        text: str,
        entity_type: Optional[str] = None,
        entity_type_category: Optional[str] = None,
        value_constraints: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Parse into typed fields (mapped straight onto the database columns)"""
        result = self.parse(text, entity_type, entity_type_category, value_constraints)

        if not result:
            result = {
                "type": "text",
                "raw": text or "",
                "value": text or "",
                "unit": None,
                "confidence": 1.0,
            }

        typed_fields = {
            "value_type": result["type"],
            "value_raw": result["raw"],
            "int_value": None,
            "float_value": None,
            "datetime_value": None,
            "bool_value": None,
            "enum_value": None,
            "value_unit": result.get("unit"),
            "value_confidence": Decimal(str(result.get("confidence", 1.0))),
        }

        value = result.get("value")
        if result["type"] == "int":
            typed_fields["int_value"] = value
        elif result["type"] == "float":
            typed_fields["float_value"] = Decimal(str(value))
        elif result["type"] == "datetime":
            typed_fields["datetime_value"] = value
        elif result["type"] == "bool":
            typed_fields["bool_value"] = value
        elif result["type"] == "enum":
            typed_fields["enum_value"] = value

        return typed_fields
