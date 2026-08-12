"""DataEngine - the single public entry point.

It orchestrates the load / extract / search modules directly, offers a clean lifecycle, and returns result models decoupled from the core.

Usage::

    async with DataEngine(config) as engine:
        ing = await engine.ingest("doc.md")
        await engine.extract()
        res = await engine.search("Who founded X?", strategy="multi", top_k=10)
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alicecore import _bootstrap
from alicecore.core import resources
from alicecore.results import ChunkItem, ChunkResult, ExtractResult, IngestResult, SearchResult

if TYPE_CHECKING:
    from alicecore.config import EngineConfig
    from alicecore.core.resources import ResourceContainer

# Facade strategy name -> (core top-level RerankStrategy value, MultiConfig sub-strategy | None).
# Declared in one place so branches and magic strings do not scatter through the method bodies.
_STRATEGY_ALIASES: dict[str, tuple[str, str | None]] = {
    "vector": ("vector", None),
    "atomic": ("atomic", None),
    "multi": ("multi", "multi"),
    "multi1": ("multi", "multi1"),
    "hopllm": ("multi", "hopllm"),
    "multi_es": ("multi_es", None),
}

# Generic default entity types (lowercase type, matching the extraction prompt convention). Seeded idempotently when a brand new
# database first creates its tables, so entities the LLM extracts are not filtered out as an "invalid type" and the entity graph works out of the box.
_DEFAULT_ENTITY_TYPES: list[tuple[str, str, str]] = [
    ("person", "Person", "Person / individual"),
    ("organization", "Organization", "Organisation / institution / company"),
    ("location", "Location", "Place / geographic location"),
    ("product", "Product", "Product / service / project"),
    ("event", "Event", "Event / activity"),
    ("time", "Time", "Time / date / period"),
    ("concept", "Concept", "Concept / term / topic"),
    ("work", "Work", "Work / document / deliverable"),
    ("technology", "Technology", "Technology / method / tool"),
    ("metric", "Metric", "Metric / value / measure"),
]


class DataEngine:
    """SAG data engine. One instance is bound to one data source (source_config_id)."""

    def __init__(
        self,
        config: EngineConfig,
        source_config_id: str | None = None,
        *,
        health_check: bool = True,
    ) -> None:
        self._config = config
        self._source_config_id = source_config_id
        self._health_check = health_check
        self._started = False

        # Runtime components (ready after start)
        self._resources: ResourceContainer | None = None
        self._resources_token: Any = None
        self._session_factory: Any = None
        self._loader: Any = None
        self._extractor: Any = None
        self._searcher: Any = None
        self._last_chunk_ids: list[str] = []

    # -- Lifecycle --------------------------------------------
    async def start(self) -> None:
        if self._started:
            return

        _bootstrap.apply_config_to_env(self._config)
        _bootstrap.reset_core_singletons()  # drop stale global connections so this engine's configuration really applies
        _bootstrap.ensure_local_dirs(self._config)
        _bootstrap.warmup_prompts()

        # Build the resource container and make it the current context (the single source of truth for DI)
        self._resources = resources.build_container(self._config)
        self._resources_token = resources.set_resources(self._resources)

        try:
            # Imported lazily to keep `import alicecore` light
            from alicecore.core.prompt.manager import get_prompt_manager
            from alicecore.modules.extract.extractor import EventExtractor
            from alicecore.modules.load.loader import DocumentLoader
            from alicecore.modules.search.searcher import SAGSearcher

            # fail-fast: check storage backend connectivity at startup, give a clear error on failure, never enter a half-initialised state
            if self._health_check:
                await self._resources.relational.ping()
                await self._resources.vector.ping()

            prompt_manager = get_prompt_manager()
            # The engine's own DB access resolves through the relational adapter (during the transition the global engine is still reused underneath, with unchanged behaviour)
            self._session_factory = self._resources.relational.session_factory()
            self._loader = DocumentLoader()
            self._extractor = EventExtractor(prompt_manager=prompt_manager)
            self._searcher = SAGSearcher(prompt_manager=prompt_manager)
            # Zero infrastructure: a local SQLite database creates its tables on first start (idempotent), so init_schema() is not needed by hand.
            # Production backends (mysql/postgres) do not auto-create tables; call init_schema() explicitly or use an Alembic migration.
            rel = self._config.relational
            if rel is not None and rel.provider == "sqlite":
                await self._create_all_schema()
            self._started = True
        except Exception:
            # Startup failed: roll the context and resources back, leaving nothing half-initialised
            await self._rollback_start()
            raise

    async def _rollback_start(self) -> None:
        """Cleanup when start fails: reset the ContextVar and release any global connection already created."""
        if self._resources_token is not None:
            resources.reset_resources(self._resources_token)
            self._resources_token = None
        self._resources = None
        self._session_factory = self._loader = self._extractor = self._searcher = None
        try:
            await _bootstrap.close_core_resources()
        except Exception:
            pass

    async def aclose(self) -> None:
        if not self._started:
            return
        await _bootstrap.close_core_resources()
        if self._resources_token is not None:
            resources.reset_resources(self._resources_token)
            self._resources_token = None
        self._resources = None
        self._session_factory = self._loader = self._extractor = self._searcher = None
        self._started = False

    @property
    def resources(self) -> ResourceContainer:
        """The resource container of this engine (its set of adapters); raises when not started."""
        if self._resources is None:
            raise RuntimeError("DataEngine is not started: await engine.start() first, or use async with")
        return self._resources

    async def __aenter__(self) -> DataEngine:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    @property
    def source_config_id(self) -> str | None:
        """The data source ID currently bound (settled on the first write or search)."""
        return self._source_config_id

    async def init_schema(self) -> None:
        """Create the missing tables (idempotent: only additions, never a drop).

        The local SQLite backend already creates its tables during ``start()``, so this is usually unnecessary; call it on a
        production backend (mysql/postgres) for the first initialisation, or manage migrations with Alembic (the ``[migrations]`` extra).
        """
        self._require_started()
        await self._create_all_schema()

    async def _create_all_schema(self) -> None:
        """Idempotent table creation (create_all, additions only) + entity type seeding; shared by start() and init_schema()."""
        import alicecore.db.models  # noqa: F401  registers every ORM model on Base.metadata
        from alicecore.db.base import Base, get_engine

        async with get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # Built-in default types: seeded only when the database holds no global entity type at all (an existing production set is never overwritten, so the entity graph works out of the box)
        await self._seed_entity_types(_DEFAULT_ENTITY_TYPES, only_if_empty=True)
        # User-defined types: appended one by one idempotently (existing type names are skipped, only new ones are added)
        if self._config.entity_types:
            specs = [
                (e.type, e.name or e.type.capitalize(), e.description)
                for e in self._config.entity_types
            ]
            await self._seed_entity_types(specs, only_if_empty=False)

    @staticmethod
    async def _seed_entity_types(specs: list[tuple[str, str, str]], only_if_empty: bool) -> None:
        """Seed the global entity types idempotently (is_default=True).

        Extraction keeps only entities of an "already defined entity type"; declaring the types lets the LLM extract and keep them.

        - ``only_if_empty=True``: seed only when the database holds no global type at all (the built-in default set, so it cannot clash with an existing production set);
        - ``only_if_empty=False``: append by ``type`` name one by one, skipping existing ones (custom types, "only add what is new").
        """
        import uuid

        from sqlalchemy import select

        from alicecore.db.base import get_session_factory
        from alicecore.db.models import EntityType

        factory = get_session_factory()
        async with factory() as session:
            existing = {
                r[0]
                for r in (
                    await session.execute(
                        select(EntityType.type).where(EntityType.scope == "global")
                    )
                ).all()
            }
            if only_if_empty and existing:
                return  # global types already exist -> leave the built-in default set alone
            added = 0
            for type_id, name, description in specs:
                if type_id in existing:
                    continue  # already exists -> skip
                session.add(
                    EntityType(
                        id=str(uuid.uuid4()),
                        scope="global",
                        source_config_id=None,
                        type=type_id,
                        name=name,
                        description=description,
                        is_default=True,
                        is_active=True,
                    )
                )
                existing.add(type_id)
                added += 1
            if added:
                await session.commit()

    # -- Writing / extraction ----------------------------------
    @staticmethod
    def _read_content(source: str | Path) -> str:
        """Resolve the argument into text: read the file when it is an existing file path, otherwise treat it as text content."""
        if isinstance(source, Path):
            return source.read_text(encoding="utf-8")
        try:
            candidate = Path(source)
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        except OSError:
            pass  # too long or an illegal path -> treat it as text content
        return source

    async def chunk(
        self,
        source: str | Path,
        *,
        max_tokens: int | None = None,
        chunk_mode: str | None = None,
    ) -> ChunkResult:
        """Split only, store nothing: parse a document or string, chunk it, and return in-memory chunks.

        Nothing is persisted, no vectors are built and **start() is not required** - pure CPU, good for a chunk preview or "chunk here, hand off elsewhere".
        ``source`` is read as a file when it is an existing file path, otherwise it is treated as text content.
        """
        from alicecore.modules.load.parser import MarkdownParser

        content = self._read_content(source)

        parser_kwargs: dict[str, Any] = {}
        if max_tokens is not None:
            parser_kwargs["max_tokens"] = max_tokens
        if chunk_mode is not None:
            parser_kwargs["chunk_mode"] = chunk_mode

        result = await MarkdownParser(**parser_kwargs).parse_content_with_plan_async(content)
        chunks = [
            ChunkItem(
                heading=getattr(c, "heading", "") or "",
                content=getattr(c, "content", "") or "",
                raw_content=getattr(c, "raw_content", None),
                rank=getattr(c, "rank", 0) or 0,
                chunk_type=getattr(c, "chunk_type", None),
            )
            for c in result.source_chunks
        ]
        return ChunkResult(chunk_count=len(chunks), chunks=chunks)

    async def ingest(
        self,
        path: str | Path,
        *,
        max_tokens: int | None = None,
        chunk_mode: str | None = None,
        background: str | None = None,
    ) -> IngestResult:
        """Load one document: parse -> chunk -> store (MySQL) -> vectorise (ES)."""
        self._require_started()
        from alicecore.modules.load.config import DocumentLoadConfig

        source_id = await self._ensure_source()
        kwargs: dict[str, Any] = {"path": str(path), "source_config_id": source_id}
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if chunk_mode is not None:
            kwargs["chunk_mode"] = chunk_mode
        if background is not None:
            kwargs["background"] = background

        result = await self._loader.load(DocumentLoadConfig(**kwargs))
        self._last_chunk_ids = list(result.chunk_ids)
        return IngestResult(
            source_config_id=result.source_config_id,
            source_id=result.source_id,
            chunk_count=result.chunk_count,
            chunk_ids=list(result.chunk_ids),
        )

    async def extract(self, *, max_concurrency: int | None = None) -> ExtractResult:
        """Extract events / entities (LLM). Requires a prior ingest (the load result of the same instance is reused)."""
        self._require_started()
        if not self._last_chunk_ids:
            raise RuntimeError("There is no chunk to extract from: await engine.ingest(...) first")
        from alicecore.modules.extract.config import ExtractBaseConfig, ExtractConfig

        source_id = await self._ensure_source()
        base_kwargs: dict[str, Any] = {}
        if max_concurrency is not None:
            base_kwargs["max_concurrency"] = max_concurrency
        base = ExtractBaseConfig(**base_kwargs)

        events = await self._extractor.extract(
            ExtractConfig(
                source_config_id=source_id,
                chunk_ids=self._last_chunk_ids,
                **base.model_dump(),
            )
        )
        return ExtractResult(
            source_config_id=source_id,
            event_count=len(events),
            event_ids=[e.id for e in events],
        )

    # -- Retrieval ---------------------------------------------
    async def search(
        self,
        query: str,
        *,
        strategy: str = "multi",
        top_k: int = 10,
    ) -> SearchResult:
        """Search and return the list of sections."""
        self._require_started()
        from alicecore.modules.search.config import SearchConfig

        base = self._build_search_config(query, strategy, top_k)
        # Strategy x backend capability check: multi_es needs real BM25 (pgvector, for instance, only degrades to ILIKE), so fail explicitly
        if _STRATEGY_ALIASES[strategy][0] == "multi_es":
            from alicecore.core.adapters import Capability, require_capability

            require_capability(
                self.resources.vector, Capability.LEXICAL_SEARCH, context=f"strategy={strategy}"
            )
        source_id = await self._ensure_source()
        search_config = SearchConfig(
            source_config_id=source_id,
            article_id=None,
            strategy_config=base.strategy_config,
            **{k: v for k, v in base.model_dump().items() if k != "strategy_config"},
        )

        raw = await self._searcher.search(search_config)
        sections = [s if isinstance(s, dict) else {"content": s} for s in raw.get("sections", [])]
        return SearchResult(query=query, sections=sections, stats=raw.get("stats", {}))

    # -- Internal ----------------------------------------------
    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("DataEngine is not started: await engine.start() first, or use async with")

    async def _ensure_source(self) -> str:
        """Make sure the SourceConfig exists and return its source_config_id (creating it when absent)."""
        from sqlalchemy import select

        from alicecore.db import SourceConfig

        if not self._source_config_id:
            self._source_config_id = str(uuid.uuid4())

        async with self._session_factory() as session:
            exists = await session.execute(
                select(SourceConfig.id).where(SourceConfig.id == self._source_config_id)
            )
            if exists.scalar_one_or_none() is None:
                session.add(
                    SourceConfig(
                        id=self._source_config_id,
                        name=f"sag-{uuid.uuid4().hex[:8]}",
                        description="created by alicecore DataEngine",
                        target_config={},
                    )
                )
                await session.commit()
        return self._source_config_id

    @staticmethod
    def _build_search_config(query: str, strategy: str, top_k: int) -> Any:
        if strategy not in _STRATEGY_ALIASES:
            raise ValueError(f"Unknown strategy '{strategy}', choose from: {', '.join(_STRATEGY_ALIASES)}")
        from alicecore.modules.search.config import (
            AtomicConfig,
            MultiConfig,
            RerankConfig,
            RerankStrategy,
            ReturnType,
            SearchBaseConfig,
            VectorConfig,
        )

        top_strategy, sub_strategy = _STRATEGY_ALIASES[strategy]
        # Bounded by the core configuration: rerank_top_k in [1,20], max_sections in [1,50].
        rerank_top_k = max(1, min(top_k, 20))
        max_sections = max(1, min(top_k, 50))

        strategy_config: Any
        return_type = ReturnType.EVENT
        if top_strategy == "vector":
            strategy_config = VectorConfig(top_k=top_k)
            return_type = ReturnType.PARAGRAPH
        elif top_strategy == "atomic":
            strategy_config = AtomicConfig(rerank_top_k=rerank_top_k, max_sections=max_sections)
        else:  # multi / multi_es
            strategy_config = MultiConfig(
                strategy=sub_strategy or "multi",
                rerank_top_k=rerank_top_k,
                max_sections=max_sections,
            )

        return SearchBaseConfig(
            query=query,
            rerank=RerankConfig(strategy=RerankStrategy(top_strategy)),
            strategy_config=strategy_config,
            return_type=return_type,
        )
