"""
Database base module

Provides the SQLAlchemy Base class and the database initialisation helpers
"""

from typing import AsyncIterator, Optional

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from alicecore.core.config import get_settings
from alicecore.utils import get_logger

logger = get_logger("db.base")

# Naming convention (used to generate constraint names automatically)
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """SQLAlchemy base class"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# Global engine and session factory
_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def get_engine() -> AsyncEngine:
    """
    Get the database engine (singleton)

    Returns:
        An AsyncEngine instance
    """
    global _engine
    if _engine is None:
        settings = get_settings()
        provider = (settings.db_provider or "mysql").lower()
        url = settings.database_url
        echo = settings.log_level == "DEBUG"

        if provider == "sqlite":
            # SQLite uses the default pool (pool_size/overflow are unsupported); foreign keys are enabled on connect
            from sqlalchemy import event

            _engine = create_async_engine(url, echo=echo, pool_pre_ping=True)

            @event.listens_for(_engine.sync_engine, "connect")
            def _enable_sqlite_fk(dbapi_conn, _record):  # pragma: no cover - a simple PRAGMA
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        else:
            # MySQL needs the UTC time zone set; PostgreSQL sets it through server_settings
            if provider in ("postgres", "postgresql"):
                connect_args = {"server_settings": {"timezone": "UTC"}}
            else:
                connect_args = {"init_command": "SET time_zone='+00:00'"}
            _engine = create_async_engine(
                url,
                echo=echo,
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                pool_pre_ping=True,
                pool_recycle=settings.db_pool_recycle,
                pool_timeout=60,
                connect_args=connect_args,
            )
        logger.info(
            "Database engine created",
            extra={"provider": provider, "database": settings.mysql_database},
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """
    Get the session factory (singleton)

    Returns:
        An async_sessionmaker instance
    """
    global _session_factory
    if _session_factory is None:
        engine = get_engine()
        _session_factory = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            autocommit=False,
        )
        logger.info("Session factory created")
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """
    Get a database session (for dependency injection)

    Yields:
        An AsyncSession instance

    Example:
        >>> async with get_session() as session:
        ...     result = await session.execute(select(User))
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_database(allow_drop: bool = False) -> None:
    """
    Initialise the database (create every table).

    By default it **only creates the missing tables** (idempotent, never deletes data). Only an explicit ``allow_drop=True``
    **drops and recreates every table** - extremely dangerous, and meant only for an explicit reset in development or testing.
    In production use an Alembic migration instead (see ``migrations/``).

    Example:
        >>> await init_database()               # safe: only creates what is missing
        >>> await init_database(allow_drop=True)  # dangerous: wipes and rebuilds (development only)
    """
    # Make sure every model has been imported and registered on Base.metadata
    from alicecore.db import models  # noqa: F401

    engine = get_engine()

    logger.info(f"Found {len(Base.metadata.tables)} table definitions")

    async with engine.begin() as conn:
        if allow_drop:
            await conn.run_sync(Base.metadata.drop_all)
            logger.warning("Dropped every old table (allow_drop=True)")

        # Create every table (only what is missing)
        await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables are ready")

    logger.info("Database initialised")


def reset_engine() -> None:
    """
    Reset the global engine and session factory (synchronous version)

    Used by a Celery worker and similar: every asyncio.run() creates a new event loop,
    while the old engine's connection pool is bound to the closed loop, so it must be discarded and rebuilt.

    sync_engine.dispose() closes every DB connection in the pool synchronously,
    which avoids leaking TCP connections because GC cannot await the async close().
    """
    global _engine, _session_factory
    if _engine is not None:
        _engine.sync_engine.dispose()
    _engine = None
    _session_factory = None


async def close_database() -> None:
    """
    Close the database connections

    Example:
        >>> await close_database()
    """
    global _engine, _session_factory

    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("Database connections closed")
