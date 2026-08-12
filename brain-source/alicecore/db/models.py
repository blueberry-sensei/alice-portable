"""
SQLAlchemy ORM model definitions

Definitions of every database table
"""

# pylint: disable=not-callable
# SQLAlchemy's func.now() is callable at runtime but Pylint doesn't recognize it

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    VARBINARY,
    or_,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from alicecore.db.base import Base

# -- Portable column types ---------------------------------------
# MySQL keeps its dedicated types (the generated DDL stays byte-for-byte identical to production, guarded by the DDL snapshot);
# PostgreSQL / SQLite fall back to generic types (TEXT / BYTEA / BLOB).
_LongText = Text().with_variant(LONGTEXT(), "mysql")
_MediumText = Text().with_variant(MEDIUMTEXT(), "mysql")
_VarBinary512 = LargeBinary(512).with_variant(VARBINARY(512), "mysql")


class SourceConfig(Base):
    """Source configuration table"""

    __tablename__ = "source_config"

    # Primary key: UUID
    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)

    # Basic source information
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(255))


    target_config: Mapped[Optional[dict]] = mapped_column(JSON)

    # Timestamps
    created_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_time: Mapped[Optional[datetime]] = mapped_column(DateTime, onupdate=func.now())

    # Relationships
    articles: Mapped[List["Article"]] = relationship(
        "Article",
        back_populates="source",
        cascade="all, delete-orphan",
    )
    source_events: Mapped[List["SourceEvent"]] = relationship(
        "SourceEvent",
        back_populates="source",
        cascade="all, delete-orphan",
    )
    entity_types: Mapped[List["EntityType"]] = relationship(
        "EntityType",
        back_populates="source",
        cascade="all, delete-orphan",
    )
    entities: Mapped[List["Entity"]] = relationship(
        "Entity",
        back_populates="source",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<SourceConfig(id={self.id}, name={self.name})>"


class KBDocument(Base):
    """Knowledge base document table (compatible with the main system's kb_document shape; no foreign key for now)"""

    __tablename__ = "kb_document"
    __table_args__ = (
        Index("idx_kb_document_kb_source", "knowledge_base_id", "source_id"),
        Index("idx_kb_document_source_id", "source_id"),
        Index("idx_kb_document_knowledge_base_id", "knowledge_base_id"),
        Index("idx_kb_document_uploader_id", "uploader_id"),
        Index("idx_kb_document_created_time", "created_time"),
        {"comment": "document table"},
    )

    id: Mapped[str] = mapped_column(String(191), primary_key=True)
    name: Mapped[str] = mapped_column(String(191), nullable=False, comment="document name")
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, comment="file size")
    file_type: Mapped[str] = mapped_column(String(50), nullable=False, comment="file type")
    knowledge_base_id: Mapped[str] = mapped_column(String(191), nullable=False, comment="knowledge base ID")
    uploader_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="uploader ID")
    source_id: Mapped[Optional[str]] = mapped_column(
        String(191),
        nullable=True,
        comment="external source ID",
    )
    parse_status: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, comment="document parse status VAR36"
    )
    parse_task_id: Mapped[Optional[str]] = mapped_column(
        String(191), nullable=True, comment="parse task ID"
    )
    source_file_url: Mapped[Optional[str]] = mapped_column(
        String(2000), nullable=True, comment="source file address"
    )
    pdf_url: Mapped[Optional[str]] = mapped_column(
        String(2000), nullable=True, comment="pdf file url"
    )
    md_file_url: Mapped[Optional[str]] = mapped_column(
        String(2000), nullable=True, comment="markdown file url"
    )
    parse_result_url: Mapped[Optional[str]] = mapped_column(
        String(2000), nullable=True, comment="parse result address"
    )
    document_metadata: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True, comment="metadata")
    copied_from: Mapped[Optional[str]] = mapped_column(
        String(191), nullable=True, comment="copied-from record ID, recording which document this record was copied from"
    )
    created_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, comment="created at"
    )
    updated_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False, comment="updated at"
    )

    def __repr__(self) -> str:
        return f"<KBDocument(id={self.id}, name={self.name[:30]})>"


class ArticleParseStatus(str, Enum):
    """Article parse status"""

    PENDING = "PENDING"
    PARSING = "PARSING"
    PARSED = "PARSED"
    EXTRACTING = "EXTRACTING"
    COMPLETED = "COMPLETED"
    PARSE_FAILED = "PARSE_FAILED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    FAILED = "FAILED"
    PENDING_RETRY_V2 = "PENDING_RETRY_V2"


class Article(Base):
    """Article table"""

    __tablename__ = "article"

    # Primary key: UUID
    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)

    # Source configuration ID: UUID (foreign key)
    source_config_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("source_config.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )

    # Basic information
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, comment="source article ID"
    )
    summary: Mapped[Optional[str]] = mapped_column(Text)
    content: Mapped[Optional[str]] = mapped_column(_LongText)

    # Category and tags
    category: Mapped[Optional[str]] = mapped_column(String(50), index=True)
    tags: Mapped[Optional[dict]] = mapped_column(JSON)  # List[str]

    # Status: PENDING, COMPLETED, FAILED, PROCESSING
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    parse_status: Mapped["ArticleParseStatus"] = mapped_column(
        String(66), default=ArticleParseStatus.PENDING, nullable=False, comment="parse status"
    )

    sync_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="time the event sync finished"
    )

    # Processing error message (recorded on failure)
    error: Mapped[Optional[str]] = mapped_column(Text)

    # Extra data: {"url": "", "headings": []}
    extra_data: Mapped[Optional[dict]] = mapped_column(JSON)

    # Timestamps
    created_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_time: Mapped[Optional[datetime]] = mapped_column(DateTime, onupdate=func.now())
    sync_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="sync time (event extraction)"
    )

    # Relationships
    source: Mapped["SourceConfig"] = relationship(
        "SourceConfig",
        back_populates="articles",
    )
    sections: Mapped[List["ArticleSection"]] = relationship(
        "ArticleSection",
        back_populates="article",
        cascade="all, delete-orphan",
    )
    source_events: Mapped[List["SourceEvent"]] = relationship(
        "SourceEvent",
        back_populates="article",
        cascade="all, delete-orphan",
    )
    entity_types: Mapped[List["EntityType"]] = relationship(
        "EntityType",
        back_populates="article",
        cascade="all, delete-orphan",
    )

    # Indexes
    __table_args__ = (
        Index("idx_article_source_config_id", "source_config_id"),
        Index("idx_source_config_status", "source_config_id", "status"),
        Index("idx_article_source_id", "source_id"),
        Index("idx_category", "category"),
    )

    def __repr__(self) -> str:
        return f"<Article(id={self.id}, title={self.title[:30]})>"


class ArticleSection(Base):
    """Article section table"""

    __tablename__ = "article_section"

    # Primary key: UUID
    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)

    # Article ID: UUID (foreign key)
    article_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("article.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )

    # Section information
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="sort index")
    render_group_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="render group index"
    )
    type: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, comment="section type: TEXT/IMAGE/CODE/TABLE and so on"
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(_LongText, nullable=False, comment="processed content (plain text)")
    raw_content: Mapped[Optional[str]] = mapped_column(
        _LongText, nullable=True, comment="raw content (may contain markdown/html)"
    )
    image_url: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True, comment="image URL (image type only)"
    )
    length: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="content length")

    # Extra data: {"type": "TEXT|IMAGE|CODE", "length": 0}
    extra_data: Mapped[Optional[dict]] = mapped_column(JSON)

    # Timestamps
    created_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    article: Mapped["Article"] = relationship(
        "Article",
        back_populates="sections",
    )

    # Indexes
    __table_args__ = (
        Index("idx_article_section_article_id", "article_id"),
        Index("idx_article_section_article_rank", "article_id", "rank"),
        Index("idx_article_order", "article_id", "order_index"),
    )

    def __repr__(self) -> str:
        return f"<ArticleSection(id={self.id}, heading={self.heading[:30]})>"


class EntityType(Base):
    """Entity type definition table"""

    __tablename__ = "entity_type"

    # Primary key: UUID
    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)

    # Scope: global/source/article
    scope: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="global",
        index=True,
        comment="scope: global/source/article",
    )

    # Source configuration ID: NULL means a system default type (foreign key)
    source_config_id: Mapped[Optional[str]] = mapped_column(
        CHAR(36),
        ForeignKey("source_config.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=True,
        index=True,
    )

    # Document ID: only set when scope=article (foreign key)
    article_id: Mapped[Optional[str]] = mapped_column(
        CHAR(36),
        ForeignKey("article.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=True,
        index=True,
        comment="document ID (only set when scope=article)",
    )

    # Type identifier: time, location, person and so on
    type: Mapped[str] = mapped_column(String(50), nullable=False)

    # Type name (display name)
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    # Type description
    description: Mapped[Optional[str]] = mapped_column(Text)

    # Default weight (0.00-9.99)
    weight: Mapped[Decimal] = mapped_column(Numeric(3, 2), default=Decimal("1.00"), nullable=False)

    # Similarity match threshold (0.000-1.000) - the minimum similarity required for entity vector search and deduplication
    similarity_threshold: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), default=Decimal("0.800"), nullable=False
    )

    # Whether it is enabled
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Whether it is a system default type
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Value format template (such as "{number}{unit}")
    value_format: Mapped[Optional[str]] = mapped_column(String(100))

    # Value constraint (JSON, holding an enum list, a numeric range and so on)
    value_constraints: Mapped[Optional[dict]] = mapped_column(JSON)

    # Extra data: {"extraction_prompt": "", "validation_rule": {}}
    extra_data: Mapped[Optional[dict]] = mapped_column(JSON)

    # Timestamps
    created_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    source: Mapped[Optional["SourceConfig"]] = relationship(
        "SourceConfig",
        back_populates="entity_types",
    )
    article: Mapped[Optional["Article"]] = relationship(
        "Article",
        back_populates="entity_types",
    )
    entities: Mapped[List["Entity"]] = relationship(
        "Entity",
        back_populates="entity_type",
    )

    # Unique constraints and indexes
    __table_args__ = (
        Index(
            "uk_scope_source_config_article_type",
            "scope",
            "source_config_id",
            "article_id",
            "type",
            unique=True,
        ),
        Index("idx_default_active", "is_default", "is_active"),
    )

    def __repr__(self) -> str:
        return f"<EntityType(id={self.id}, type={self.type}, name={self.name})>"


class Entity(Base):
    """Entity table (many-to-many: linked to events through the event_entity association table)"""

    __tablename__ = "entity"

    # Primary key: UUID
    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)

    # Source configuration ID: UUID (foreign key)
    source_config_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("source_config.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )

    # Entity type ID: UUID (foreign key)
    entity_type_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("entity_type.id", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )

    # Type identifier (denormalised for easier querying)
    type: Mapped[str] = mapped_column(String(50), nullable=False)

    # Entity information
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(500), nullable=False, index=True)

    # Description
    description: Mapped[Optional[str]] = mapped_column(Text)

    # ========== Typed value fields (for statistical analysis) ==========

    # Value type marker (int/float/datetime/bool/enum/text)
    value_type: Mapped[Optional[str]] = mapped_column(String(20), index=True)

    # Raw extracted text (the original value is kept, such as "199 USD")
    value_raw: Mapped[Optional[str]] = mapped_column(Text)

    # Integer value field
    int_value: Mapped[Optional[int]] = mapped_column(BigInteger, index=True)

    # Floating point value field (DECIMAL to keep the precision)
    float_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), index=True)

    # Datetime value field
    datetime_value: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)

    # Boolean value field
    bool_value: Mapped[Optional[bool]] = mapped_column(Boolean)

    # Enum value field
    enum_value: Mapped[Optional[str]] = mapped_column(String(100), index=True)

    # Unit field (such as "USD", "EUR", "kg")
    value_unit: Mapped[Optional[str]] = mapped_column(String(50))

    # Parse confidence (0-1)
    value_confidence: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4))

    # Extra data: {"synonyms": [], "weight": 1.0, "confidence": 1.0}
    extra_data: Mapped[Optional[dict]] = mapped_column(JSON)

    # Timestamps
    created_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    source: Mapped["SourceConfig"] = relationship(
        "SourceConfig",
        back_populates="entities",
    )
    entity_type: Mapped["EntityType"] = relationship(
        "EntityType",
        back_populates="entities",
    )
    # Many-to-many: through the event_entity association table
    event_associations: Mapped[List["EventEntity"]] = relationship(
        "EventEntity",
        back_populates="entity",
        cascade="all, delete-orphan",
    )

    # Unique constraints and indexes
    __table_args__ = (
        Index(
            "uk_source_config_type_name", "source_config_id", "type", "normalized_name", unique=True
        ),
        Index("idx_entity_source_config_id", "source_config_id"),
        Index("idx_entity_type_id", "entity_type_id"),
        Index("idx_normalized_name", "normalized_name"),
        Index("idx_source_config_type", "source_config_id", "type"),
        # Composite index on the typed values (for statistical queries)
        Index("ix_entity_type_value_type", "type", "value_type"),
        Index("ix_entity_source_config_value_type", "source_config_id", "value_type"),
    )

    def __repr__(self) -> str:
        return f"<Entity(id={self.id}, name={self.name}, type={self.type})>"


class EventEntity(Base):
    """Event-entity association table (many-to-many)"""

    __tablename__ = "event_entity"
    __table_args__ = (
        Index("uk_event_entity", "event_id", "entity_id", unique=True),
        Index("idx_event_id", "event_id"),
        Index("idx_entity_id", "entity_id"),
    )

    # Primary key: UUID
    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)

    # Event ID: UUID (foreign key)
    event_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("source_event.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )

    # Entity ID: UUID (foreign key)
    entity_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("entity.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )

    # Weight of this entity within this event (0.00-9.99)
    weight: Mapped[Decimal] = mapped_column(Numeric(3, 2), default=Decimal("1.00"))

    # Description or role of this entity within this event (such as "CEO of a company", "angel investor")
    description: Mapped[Optional[str]] = mapped_column(Text)

    # Extra data: {"confidence": 0.95, "context": ""}
    extra_data: Mapped[Optional[dict]] = mapped_column(JSON)

    # Timestamps
    created_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # Relationships
    event: Mapped["SourceEvent"] = relationship(
        "SourceEvent",
        back_populates="event_associations",
        lazy="noload",  # prevents a lazy-loading error
    )
    entity: Mapped["Entity"] = relationship(
        "Entity",
        back_populates="event_associations",
        lazy="noload",  # prevents a lazy-loading error
    )
    embedding: Mapped[Optional["EventEntityEmbedding"]] = relationship(
        "EventEntityEmbedding",
        back_populates="event_entity",
        lazy="noload",
        uselist=False,
    )

    def __repr__(self) -> str:
        return f"<EventEntity(event_id={self.event_id}, entity_id={self.entity_id}, weight={self.weight})>"


class EventEntityEmbedding(Base):
    """Event entity vector table (one to one)"""

    __tablename__ = "event_entity_embedding"
    __table_args__ = (
        ForeignKeyConstraint(
            ["id"],
            ["event_entity.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
            name="fk_event_entity_embedding_event_entity",
        ),
        Index("idx_event_entity_embedding_updated_time", "updated_time"),
        {"comment": "event entity vector table (one to one)"},
    )

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True, comment="references event_entity.id")
    vec: Mapped[bytes] = mapped_column(
        _VarBinary512,
        nullable=False,
        comment="128-dim float32 embedding bytes",
    )
    created_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_time: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    event_entity: Mapped["EventEntity"] = relationship(
        "EventEntity",
        back_populates="embedding",
        lazy="noload",
        uselist=False,
    )


class SourceEvent(Base):
    """Source event table"""

    __tablename__ = "source_event"

    # Primary key: UUID
    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)

    # Source configuration ID: UUID (foreign key)
    source_config_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("source_config.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )

    # Source markers (polymorphic fields, one interface)
    source_type: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True, comment="source type: ARTICLE/CHAT"
    )
    source_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True, comment="source ID"
    )

    # Article ID: UUID (foreign key)
    article_id: Mapped[Optional[str]] = mapped_column(
        CHAR(36),
        ForeignKey("article.id", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=True,
        index=True,
    )

    # Event information
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(_MediumText, nullable=False)
    content: Mapped[str] = mapped_column(_LongText, nullable=False)

    # Event category (such as technology, product, market, research, management)
    category: Mapped[Optional[str]] = mapped_column(String(50), default="")

    # Keyword list
    keywords: Mapped[Optional[dict]] = mapped_column(JSON, comment="keyword list")

    # Business fields (compatible with the main system)
    type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    priority: Mapped[Optional[str]] = mapped_column(String(50), default="")
    status: Mapped[Optional[str]] = mapped_column(String(50), default="")

    # Sort ordinal
    rank: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Hierarchy fields
    level: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="hierarchy depth (0=top level)"
    )
    parent_id: Mapped[Optional[str]] = mapped_column(
        CHAR(36),
        ForeignKey("source_event.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=True,
        index=True,
        comment="parent event ID (self reference)",
    )

    # Time range
    start_time: Mapped[Optional[datetime]] = mapped_column(DateTime)
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # Reference to the raw section
    references: Mapped[Optional[dict]] = mapped_column(JSON)

    # Source chunk ID: UUID (points at SourceChunk)
    chunk_id: Mapped[Optional[str]] = mapped_column(CHAR(36), index=True)

    # Extra data: {"keywords": [], "category": "", "priority": "", "status": ""}
    extra_data: Mapped[Optional[dict]] = mapped_column(JSON)

    # Timestamps
    created_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    source: Mapped["SourceConfig"] = relationship(
        "SourceConfig",
        back_populates="source_events",
    )
    article: Mapped[Optional["Article"]] = relationship(
        "Article",
        back_populates="source_events",
    )
    # Many-to-many: through the event_entity association table
    event_associations: Mapped[List["EventEntity"]] = relationship(
        "EventEntity",
        back_populates="event",
        cascade="all, delete-orphan",
    )
    # Hierarchy: parent and child events (self reference)
    parent: Mapped[Optional["SourceEvent"]] = relationship(
        "SourceEvent",
        remote_side="SourceEvent.id",
        back_populates="children",
    )
    children: Mapped[List["SourceEvent"]] = relationship(
        "SourceEvent",
        back_populates="parent",
        cascade="all, delete-orphan",
    )

    @property
    def entities(self) -> List["Entity"]:
        """Access the entity list through the association table"""
        return [assoc.entity for assoc in self.event_associations]

    # Indexes
    # Note: MySQL does not support a CHECK constraint on a column that has a foreign key action, so data integrity is enforced in the application layer
    __table_args__ = (
        Index("idx_source_event_source_config_id", "source_config_id"),
        Index("idx_source_event_source", "source_type", "source_id"),
        Index("idx_source_rank", "source_type", "source_id", "rank"),
        Index("idx_source_event_article_id", "article_id"),
        Index("idx_source_event_article_rank", "article_id", "rank"),
        Index("idx_chunk_id", "chunk_id"),
        Index("idx_parent_id", "parent_id"),
        Index("idx_level", "level"),
        Index("idx_parent_level", "parent_id", "level"),
        Index("idx_start_time", "start_time"),
        Index("idx_end_time", "end_time"),
    )

    def __repr__(self) -> str:
        return f"<SourceEvent(id={self.id}, title={self.title[:30]})>"

    @classmethod
    def not_deleted(cls):
        return or_(cls.status.is_(None), cls.status != "DELETED")



class SourceChunk(Base):
    """
    Source chunk aggregation table - aggregates ArticleSection sentences or ChatMessage sentences into chunks
    """

    __tablename__ = "source_chunk"

    # Fields with a default - primary key
    id: Mapped[str] = mapped_column(
        CHAR(36),
        primary_key=True,
        default=lambda: str(__import__("uuid").uuid4()),
    )

    # Source configuration ID (required, foreign key)
    source_config_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("source_config.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )

    # Source markers (polymorphic fields, the main ones used)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    # Foreign key fields (cascade delete)
    article_id: Mapped[Optional[str]] = mapped_column(
        CHAR(36),
        ForeignKey("article.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=True,
        index=True,
    )

    # Optional fields (no default but nullable)
    content: Mapped[Optional[str]] = mapped_column(_LongText, nullable=True)
    extra_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    references: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    heading: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    raw_content: Mapped[Optional[str]] = mapped_column(_LongText, nullable=True)

    # Fields with a default
    rank: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_length: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_time: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_time: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    source_config: Mapped["SourceConfig"] = relationship("SourceConfig")
    article: Mapped[Optional["Article"]] = relationship("Article")

    # Indexes
    __table_args__ = (
        Index("idx_source_chunk_source", "source_type", "source_id", "rank"),
        Index("idx_source_chunk_source_config_id", "source_config_id"),
        Index("idx_source_chunk_article_id", "article_id"),
        Index("idx_created", "created_time"),
        {"comment": "source chunk aggregation table - aggregates ArticleSection into chunks"},
    )

    def __repr__(self) -> str:
        return f"<SourceChunk(id={self.id}, source_type={self.source_type}, source_id={self.source_id})>"


__all__ = [
    "SourceConfig",
    "KBDocument",
    "ArticleParseStatus",
    "Article",
    "ArticleSection",
    "EntityType",
    "Entity",
    "EventEntity",
    "EventEntityEmbedding",
    "SourceEvent",
    "SourceChunk",
]
