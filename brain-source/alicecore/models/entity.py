"""
Entity Data Models
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import Field, field_validator

from alicecore.models.base import pipelineBaseModel, MetadataMixin, TimestampMixin


class EntityType(pipelineBaseModel, MetadataMixin, TimestampMixin):
    """Entity type definition model"""

    id: str = Field(..., description="Entity type ID (UUID)")
    scope: str = Field(
        default="global", description="Scope: global/source/article")
    source_config_id: Optional[str] = Field(
        default=None, description="Source config ID (NULL for system default)")
    article_id: Optional[str] = Field(
        default=None, description="Article ID (only when scope=article)")
    type: str = Field(..., min_length=1, max_length=50, description="Type identifier")
    name: str = Field(..., min_length=1, max_length=100, description="Type name")
    is_default: bool = Field(default=False, description="Is system default type")
    description: Optional[str] = Field(default=None, description="Type description")
    weight: float = Field(default=1.0, ge=0.0, le=9.99, description="Default weight")
    similarity_threshold: float = Field(
        default=0.80, ge=0.0, le=1.0, description="Entity similarity threshold (0.000-1.000)"
    )
    is_active: bool = Field(default=True, description="Is active")
    value_format: Optional[str] = Field(
        default=None, description="Value format template (e.g. {number}{unit})")
    value_constraints: Optional[Dict[str, Any]] = Field(
        default=None, description="Value constraints (e.g. enum list, number range)")

    @field_validator("weight")
    @classmethod
    def validate_weight(cls, v: float) -> float:
        """Validate weight range"""
        return round(v, 2)

    @field_validator("similarity_threshold")
    @classmethod
    def validate_similarity_threshold(cls, v: float) -> float:
        """Validate similarity threshold and keep 3 decimal places"""
        return round(v, 3)


class Entity(pipelineBaseModel, MetadataMixin, TimestampMixin):
    """Entity model (many-to-many: linked to events via event_entity)"""

    id: str = Field(..., description="Entity ID (UUID)")
    source_config_id: str = Field(..., description="Source config ID")
    entity_type_id: str = Field(..., description="Entity type ID (references entity_type.id)")
    type: str = Field(
        ..., min_length=1, max_length=50, description="Entity type identifier (redundant field for query)"
    )
    name: str = Field(..., min_length=1, max_length=500, description="Entity name")
    normalized_name: str = Field(..., min_length=1,
                                 max_length=500, description="Normalized name")
    description: Optional[str] = Field(default=None, description="Entity description")

    # ========== Typed value fields (for statistical analysis) ==========
    value_type: Optional[str] = Field(
        default=None, description="Value type (int/float/datetime/bool/enum/text)")
    value_raw: Optional[str] = Field(
        default=None, description="Raw extracted text (e.g. '$199')")
    int_value: Optional[int] = Field(default=None, description="Integer value")
    float_value: Optional[Decimal] = Field(default=None, description="Float value")
    datetime_value: Optional[datetime] = Field(
        default=None, description="Datetime value")
    bool_value: Optional[bool] = Field(default=None, description="Boolean value")
    enum_value: Optional[str] = Field(default=None, description="Enum value")
    value_unit: Optional[str] = Field(
        default=None, description="Unit (e.g. 'USD', 'kg')")
    value_confidence: Optional[Decimal] = Field(
        default=None, ge=0.0, le=1.0, description="Parsing confidence")

    def get_typed_value(self) -> Any:
        """Get typed value based on value_type"""
        if self.value_type == "int":
            return self.int_value
        elif self.value_type == "float":
            return self.float_value
        elif self.value_type == "datetime":
            return self.datetime_value
        elif self.value_type == "bool":
            return self.bool_value
        elif self.value_type == "enum":
            return self.enum_value
        return None

    def get_synonyms(self) -> List[str]:
        """Get synonyms"""
        if self.extra_data and "synonyms" in self.extra_data:
            return self.extra_data["synonyms"]
        return []

    def get_weight(self) -> float:
        """Get weight"""
        if self.extra_data and "weight" in self.extra_data:
            return self.extra_data["weight"]
        return 1.0

    def get_confidence(self) -> float:
        """Get confidence"""
        if self.extra_data and "confidence" in self.extra_data:
            return self.extra_data["confidence"]
        return 1.0


class EventEntity(pipelineBaseModel, MetadataMixin, TimestampMixin):
    """Event-Entity association model (many-to-many)"""

    id: str = Field(..., description="Association ID (UUID)")
    event_id: str = Field(..., description="Event ID")
    entity_id: str = Field(..., description="Entity ID")
    weight: float = Field(default=1.0, ge=0.0, le=9.99,
                          description="Entity weight in this event")

    @field_validator("weight")
    @classmethod
    def validate_weight(cls, v: float) -> float:
        """Validate weight range"""
        return round(v, 2)

    def get_confidence(self) -> float:
        """Get confidence"""
        if self.extra_data and "confidence" in self.extra_data:
            return self.extra_data["confidence"]
        return 1.0

    def get_context(self) -> Optional[str]:
        """Get context"""
        if self.extra_data and "context" in self.extra_data:
            return self.extra_data["context"]
        return None


class CustomEntityType(pipelineBaseModel):
    """Custom entity type definition"""

    type: str = Field(..., description="Type identifier")
    name: str = Field(..., description="Type name")
    description: str = Field(..., description="Type description for LLM extraction")
    weight: float = Field(default=1.0, ge=0.0, le=9.99, description="Default weight")
    extraction_prompt: Optional[str] = Field(
        default=None, description="Custom extraction prompt template")
    extraction_examples: Optional[List[Dict[str, str]]] = Field(
        default=None, description="Few-shot examples"
    )
    validation_rule: Optional[Dict[str, Any]] = Field(
        default=None, description="Validation rule")
    metadata_schema: Optional[Dict[str, Any]] = Field(
        default=None, description="Metadata schema")


# ==============================================================================
# Default entity type definitions (based on the 5W1H framework)
# ==============================================================================
# 
# Design principles:
# 1. Ontological types - what an entity *is*, not what it is used for
# 2. Mutual exclusivity - every entity belongs to exactly one type
# 3. Completeness - covers every possible entity type
# 4. Search oriented - lets the LLM recognise a "clue dimension" and a "target dimension"
#
# Weights (layered by search importance):
# - high (1.2~1.5): subject(1.5), action(1.3), metric(1.2), person(1.2)
# - medium (1.0~1.1): organization(1.1), product(1.1), group/work/time/location(1.0)
# - catch-all (0.5): tags - keeps it from being overused
#
# Total: 11 types, covering 95%+ of question-answering cases
# - time (WHEN): time
# - space (WHERE): location
# - subject (WHO): person, organization, group
# - content (WHAT): subject, work, product
# - manner (HOW): action, metric
# - catch-all: tags
#
# ==============================================================================

DEFAULT_ENTITY_TYPES = [
    # ==========================================================================
    # [WHEN - time dimension]
    # ==========================================================================
    EntityType(
        id="30000000-0000-0000-0000-000000000001",
        scope="global",
        source_config_id=None,
        article_id=None,
        type="time",
        name="Time",
        is_default=True,
        description="The point in time, time range or period in which an event happened. Such as: a specific date, a span of time, a public holiday, a season, a decade. Examples: 2024, the third quarter, New Year, Monday.",
        weight=1.0,
        similarity_threshold=0.900,
    ),
    
    # ==========================================================================
    # [WHERE - space dimension]
    # ==========================================================================
    EntityType(
        id="30000000-0000-0000-0000-000000000002",
        scope="global",
        source_config_id=None,
        article_id=None,
        type="location",
        name="Location",
        is_default=True,
        description="A geographic location, administrative division, physical space or building. Such as: a country, city, region, landmark, venue, a historical place, a park. Examples: Beijing, New York, Tokyo, Paris, the Roman Republic.",
        weight=1.0,
        similarity_threshold=0.750,
    ),
    
    # ==========================================================================
    # [WHO - subject dimension]
    # ==========================================================================
    EntityType(
        id="30000000-0000-0000-0000-000000000003",
        scope="global",
        source_config_id=None,
        article_id=None,
        type="person",
        name="Person",
        is_default=True,
        description="One specific natural person, real or fictional. Such as: a name, stage name, pen name, historical figure, fictional character. Examples: Einstein, Shakespeare, Steve Jobs.",
        weight=1.2,
        similarity_threshold=0.950,
    ),
    EntityType(
        id="30000000-0000-0000-0000-000000000004",
        scope="global",
        source_config_id=None,
        article_id=None,
        type="organization",
        name="Organisation",
        is_default=True,
        description="The name of an institution, organisation or team. Such as: a company, government body, school, educational institution, NGO, platform, sports team, band, military organisation, political party, committee. Examples: Google, the United Nations, Harvard University.",
        weight=1.1,
        similarity_threshold=0.850,
    ),
    EntityType(
        id="30000000-0000-0000-0000-000000000005",
        scope="global",
        source_config_id=None,
        article_id=None,
        type="group",
        name="Group",
        is_default=True,
        description="An informal or abstract collection of people, or an identity label, formed from a shared demographic trait, social identity, occupation, role or behaviour. Such as: an age group, occupational group, consumer group, social identity. Examples: teenagers, doctors, users, investors.",
        weight=1.0,
        similarity_threshold=0.700,
    ),
    
    # ==========================================================================
    # [WHAT - content dimension] (the core search dimension)
    # ==========================================================================
    EntityType(
        id="30000000-0000-0000-0000-000000000006",
        scope="global",
        source_config_id=None,
        article_id=None,
        type="subject",
        name="Subject",
        is_default=True,
        description="The core topic, concept, field or phenomenon being discussed. Includes: technology trends, social phenomena, professional fields, historical events, awards, competitions and events. Examples: artificial intelligence, climate change, the Xinhai Revolution, the Nobel Prize.",
        weight=1.5,
        similarity_threshold=0.600,
    ),
    EntityType(
        id="30000000-0000-0000-0000-000000000007",
        scope="global",
        source_config_id=None,
        article_id=None,
        type="work",
        name="Work",
        is_default=True,
        description="An intellectual or cultural creation, usually marked by title punctuation or italics. Such as: a book, film or television work, piece of music, novel, paper, game. Examples: The Three-Body Problem, Star Wars, Hamlet.",
        weight=1.0,
        similarity_threshold=0.850,
    ),
    EntityType(
        id="30000000-0000-0000-0000-000000000008",
        scope="global",
        source_config_id=None,
        article_id=None,
        type="product",
        name="Product",
        is_default=True,
        description="A good or service that can be bought or used; not a technical concept, a creative work or an architecture. Such as: hardware, software, a service, a brand. Examples: iPhone, Coca-Cola, Windows.",
        weight=1.1,
        similarity_threshold=0.800,
    ),
    
    # ==========================================================================
    # [HOW - manner dimension]
    # ==========================================================================
    EntityType(
        id="30000000-0000-0000-0000-000000000009",
        scope="global",
        source_config_id=None,
        article_id=None,
        type="action",
        name="Action",
        is_default=True,
        description="An action, operation or method a subject performs. Such as: a business action, user behaviour, method or strategy, process step - the action itself rather than its result. Examples: launch, acquire, partner, invest.",
        weight=1.3,
        similarity_threshold=0.800,
    ),
    EntityType(
        id="30000000-0000-0000-0000-000000000010",
        scope="global",
        source_config_id=None,
        article_id=None,
        type="metric",
        name="Metric",
        is_default=True,
        description="A quantifiable measure; it must contain a concrete number and keep the original format (including units such as %, million, billion). Such as: a share, ratio, count, parameter size. Examples: 12%, up 31%, 1 million, 137B.",
        weight=1.2,
        similarity_threshold=0.800,
    ),
    
    # ==========================================================================
    # [Catch-all dimension]
    # ==========================================================================
    EntityType(
        id="30000000-0000-0000-0000-000000000011",
        scope="global",
        source_config_id=None,
        article_id=None,
        type="tags",
        name="Tags",
        is_default=True,
        description="An entity that fits none of the types above; used only as a catch-all.",
        weight=0.5,
        similarity_threshold=0.700,
    ),
]
