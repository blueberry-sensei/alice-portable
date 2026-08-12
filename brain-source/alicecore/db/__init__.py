"""
Database module

Provides the SQLAlchemy ORM models and database helpers
"""

from alicecore.db.base import Base, get_engine, get_session_factory, init_database, close_database, reset_engine
from alicecore.db.models import (
    Article,
    ArticleParseStatus,
    ArticleSection,
    Entity,
    EntityType,
    EventEntity,
    EventEntityEmbedding,
    KBDocument,
    SourceChunk,
    SourceConfig,
    SourceEvent,
)

__all__ = [
    # Base
    "Base",
    "get_engine",
    "get_session_factory",
    "init_database",
    "close_database",
    "reset_engine",
    # Models
    "SourceConfig",
    "Article",
    "ArticleParseStatus",
    "ArticleSection",
    "EntityType",
    "Entity",
    "EventEntity",
    "EventEntityEmbedding",
    "SourceEvent",
    "SourceChunk",
    "KBDocument",
]
