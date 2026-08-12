"""Injected configuration objects.

Design goal: make "configuration is injected from outside" the single source, replacing the core layer's implicit
dependency on a global `.env` + `get_settings()` singleton. The `_bootstrap` facade bridges this configuration into the core.

Note: this is [new facade layer] code; it neither depends on nor modifies the core `pipeline/` logic.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class MySQLConfig(BaseModel):
    """MySQL connection settings (backwards compatible; equivalent to RelationalConfig(provider="mysql"))."""

    host: str = "localhost"
    port: int = 3306
    user: str
    password: str
    database: str
    pool_size: int = 100
    max_overflow: int = 200
    pool_recycle: int = 3600


class RelationalConfig(BaseModel):
    """Relational database connection settings (the dialect can be switched).

    - ``provider="mysql"``:host/port/user/password/database
    - ``provider="postgres"``: as above (asyncpg)
    - ``provider="sqlite"``: only ``path`` (the file path) is needed, the rest is ignored
    """

    provider: Literal["mysql", "postgres", "sqlite", "oceanbase"] = "mysql"
    host: str = "localhost"
    port: int = 3306
    user: str = "sag2"
    password: str = "sag2"
    database: str = "sag2"
    path: str = ""  # sqlite file path
    pool_size: int = 100
    max_overflow: int = 200
    pool_recycle: int = 3600


class ESConfig(BaseModel):
    """Elasticsearch connection settings."""

    hosts: list[str] = Field(default_factory=lambda: ["http://localhost:9200"])
    username: str | None = None
    password: str | None = None
    timeout: int = 300
    verify_certs: bool = False


class LLMProviderConfig(BaseModel):
    """One LLM provider in the routing chain.

    Providers queue by ascending ``priority``; a call starts at the front and falls through to the next on a 429,
    a quota error or an auth failure (rules in ``core.ai.routing``). ``extra_body`` carries gateway-specific parameters,
    for example choosing an OpenRouter backend::

        LLMProviderConfig(
            id="openrouter-deepseek", api_key="sk-or-...",
            model="deepseek/deepseek-v4-flash",
            base_url="https://openrouter.ai/api/v1",
            extra_body={"provider": {"order": ["deepinfra/fp4"], "allow_fallbacks": False}},
        )
    """

    id: str
    api_key: str
    model: str
    label: str = ""
    priority: int = 100
    enabled: bool = True
    provider: Literal["openai", "litellm"] = "litellm"
    base_url: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: int | None = None
    max_retries: int | None = None
    enable_think: bool | None = None
    extra_body: dict | None = None
    #: How many seconds to skip this provider after it rate limits (so a fall-through does not keep hitting an empty quota)
    cooldown_seconds: float = 60.0


class LLMConfig(BaseModel):
    """LLM (OpenAI-compatible) settings.

    With a single provider just fill in ``api_key``/``model``. For multi-provider failover fill in ``providers``:
    the flat fields then only supply defaults for whatever an entry leaves unset (temperature/max_tokens/timeout and so on).
    """

    api_key: str = ""
    model: str = ""
    provider: Literal["openai", "litellm"] = "openai"
    base_url: str | None = None
    temperature: float = 0.7
    max_tokens: int = 30000
    timeout: int = 300
    max_retries: int = 5
    enable_think: bool = False
    extra_body: dict | None = None
    #: Priority routing chain; when empty the flat configuration above is the only provider.
    providers: list[LLMProviderConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_credentials(self) -> LLMConfig:
        """Either give flat api_key+model, or at least one enabled provider - both cannot be empty."""
        from alicecore.exceptions import ConfigError

        if self.providers:
            enabled = [p for p in self.providers if p.enabled]
            if not enabled:
                raise ConfigError("No provider in LLMConfig.providers has enabled=True")
            missing = [p.id for p in enabled if not p.api_key or not p.model]
            if missing:
                raise ConfigError(f"LLM provider is missing api_key or model: {', '.join(missing)}")
            return self
        if not self.api_key or not self.model:
            raise ConfigError("LLMConfig needs api_key + model, or a non-empty providers list")
        return self

    def resolved_chain(self) -> list[LLMProviderConfig]:
        """Return the enabled providers by priority (a flat configuration folds into a one-element chain)."""
        if not self.providers:
            return [
                LLMProviderConfig(
                    id="primary",
                    label=f"primary / {self.model}",
                    api_key=self.api_key,
                    model=self.model,
                    provider=self.provider,
                    base_url=self.base_url,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    timeout=self.timeout,
                    max_retries=self.max_retries,
                    enable_think=self.enable_think,
                    extra_body=self.extra_body,
                )
            ]
        chain = [p for p in self.providers if p.enabled]
        chain.sort(key=lambda p: p.priority)
        return [
            p.model_copy(
                update={
                    "label": p.label or f"{p.id} / {p.model}",
                    "temperature": self.temperature if p.temperature is None else p.temperature,
                    "max_tokens": self.max_tokens if p.max_tokens is None else p.max_tokens,
                    "timeout": self.timeout if p.timeout is None else p.timeout,
                    "max_retries": self.max_retries if p.max_retries is None else p.max_retries,
                    "enable_think": self.enable_think if p.enable_think is None else p.enable_think,
                }
            )
            for p in chain
        ]


class EmbeddingConfig(BaseModel):
    """Embedding settings; api_key/base_url fall back to the LLM values when left empty.

    ``max_retries``: retries against the same endpoint. Embedding does **not** fall through to another provider - switching
    provider switches the vector space, and mixing two coordinate systems in one index distorts retrieval. When the retries run out it fails (never silently skipped).
    """

    model: str
    dimensions: int | None = None
    api_key: str | None = None
    base_url: str | None = None
    max_retries: int = 3


class RerankConfig(BaseModel):
    """Rerank settings (optional)."""

    model: str
    api_key: str | None = None
    base_url: str | None = None
    endpoint: str = "/rerank"


class EntityTypeConfig(BaseModel):
    """Custom entity type (seeded idempotently when the schema is initialised; existing type names are skipped, only new ones are added).

    Extraction keeps only entities of an "already defined type"; declaring your domain types lets the LLM extract and keep them.
    ``type`` is a lowercase identifier (matching the extraction prompt convention), such as ``"contract"`` / ``"invoice"``.
    """

    type: str
    name: str | None = None  # display name; defaults to type with the first letter capitalised
    description: str = ""


class EngineConfig(BaseModel):
    """Overall engine configuration (injected).

    Usage::

        # Zero infrastructure (default): local SQLite + LanceDB written under ./.alicecore/, no service required
        config = EngineConfig(
            llm=LLMConfig(api_key="...", model="qwen3.6-flash", base_url="https://.../v1"),
            embedding=EmbeddingConfig(model="bge-large-en-v1.5", base_url="https://.../v1", api_key="..."),
        )

        # Production: switch to MySQL + Elasticsearch (needs pip install "alicecore[mysql,es]")
        config = EngineConfig(
            mysql=MySQLConfig(user="sag2", password="...", database="sag2"),
            vector_provider="es",
            es=ESConfig(hosts=["http://localhost:9200"]),
            llm=LLMConfig(api_key="...", model="qwen3.6-flash", base_url="https://.../v1"),
            embedding=EmbeddingConfig(model="bge-large-en-v1.5", base_url="https://.../v1", api_key="..."),
        )
    """

    # Relational database: left empty means local SQLite (zero infrastructure); relational wins, and a lone mysql maps to mysql.
    relational: RelationalConfig | None = None
    mysql: MySQLConfig | None = None
    es: ESConfig = Field(default_factory=ESConfig)  # only used when vector_provider=es
    # Vector store: lancedb (default, local embedded) | es | pgvector (reuses the single postgres database) | oceanbase (reuses the single OB database)
    vector_provider: Literal["lancedb", "es", "pgvector", "oceanbase"] = "lancedb"
    # Local data root: the SQLite database and the LanceDB vectors land here by default (zero-configuration start)
    data_dir: str = "./.alicecore"
    llm: LLMConfig
    embedding: EmbeddingConfig
    rerank: RerankConfig | None = None
    # Custom entity types: appended idempotently when the schema is initialised (existing ones are skipped). A string or an EntityTypeConfig both work.
    entity_types: list[EntityTypeConfig] | None = None

    language: Literal["en", "vi"] = "en"
    benchmark: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("entity_types", mode="before")
    @classmethod
    def _coerce_entity_types(cls, v: object) -> object:
        """Allow ``entity_types=["contract", ...]`` (strings) and EntityTypeConfig objects to be mixed."""
        if not isinstance(v, (list, tuple)):
            return v
        return [EntityTypeConfig(type=x) if isinstance(x, str) else x for x in v]

    @model_validator(mode="after")
    def _derive_relational(self) -> EngineConfig:
        """When relational is not given: derive mysql if mysql is present, otherwise default to local SQLite (data_dir/sag.db)."""
        if self.relational is None:
            if self.mysql is None:
                from pathlib import Path

                self.relational = RelationalConfig(
                    provider="sqlite", path=str(Path(self.data_dir) / "sag.db")
                )
                return self
            m = self.mysql
            self.relational = RelationalConfig(
                provider="mysql",
                host=m.host,
                port=m.port,
                user=m.user,
                password=m.password,
                database=m.database,
                pool_size=m.pool_size,
                max_overflow=m.max_overflow,
                pool_recycle=m.pool_recycle,
            )
        return self

    @model_validator(mode="after")
    def _fallback_embedding_to_llm(self) -> EngineConfig:
        """When embedding has no api_key/base_url of its own, fall back to the LLM values (the same gateway is the common case).

        With multi-provider routing the flat fields may be empty, so the credentials of the first provider in the chain are used - the head of
        the chain is "the default one", and using its key for embedding is the only fallback that makes sense.
        """
        head = self.llm.resolved_chain()[0]
        if self.embedding.api_key is None:
            self.embedding.api_key = self.llm.api_key or head.api_key
        if self.embedding.base_url is None:
            self.embedding.base_url = self.llm.base_url or head.base_url
        return self

    @model_validator(mode="after")
    def _validate_backend_coherence(self) -> EngineConfig:
        """The vector store and relational database must be a coherent pair; an incoherent one fails at construction with an actionable error (not deep inside a run)."""
        from alicecore.exceptions import ConfigError

        rel_provider = self.relational.provider if self.relational else None
        if self.vector_provider == "pgvector" and rel_provider not in ("postgres", "postgresql"):
            raise ConfigError(
                "vector_provider='pgvector' needs a PostgreSQL relational database: set "
                "relational=RelationalConfig(provider='postgres', ...) (pgvector reuses the same PG database)."
            )
        if self.vector_provider == "oceanbase" and rel_provider != "oceanbase":
            raise ConfigError(
                "vector_provider='oceanbase' needs an OceanBase relational database: set "
                "relational=RelationalConfig(provider='oceanbase', ...) (one database for SQL and vectors)."
            )
        if self.vector_provider == "es" and not self.es.hosts:
            raise ConfigError(
                "vector_provider='es' needs es.hosts: set es=ESConfig(hosts=['http://localhost:9200'])."
            )
        return self

    @classmethod
    def from_env(cls, env_file: str | None = None) -> EngineConfig:
        """Load the configuration from environment variables (standard flat names, no prefix).

        Secrets and endpoints follow the ecosystem's usual names (the same as the OpenAI SDK); storage uses self-describing flat names. Minimal zero-infrastructure set::

            OPENAI_API_KEY=sk-...
            OPENAI_BASE_URL=https://your-gateway/v1     # optional (defaults to OpenAI official)
            LLM_MODEL=qwen3.6-flash
            EMBEDDING_MODEL=bge-large-en-v1.5

        Switching to a production backend (example: MySQL + Elasticsearch)::

            RELATIONAL_PROVIDER=mysql  DB_HOST=localhost DB_USER=sag2 DB_PASSWORD=... DB_NAME=sag2
            VECTOR_PROVIDER=es         ES_HOSTS=http://localhost:9200

        The full variable list is in the repository's ``.env.example`` and README. When embedding has no KEY/URL of its own it falls back to OPENAI_*.

        Args:
            env_file: optional path to a ``.env`` file (``KEY=VALUE`` lines; os.environ wins over the file).
        """
        import os

        env: dict[str, str] = {}
        if env_file:
            env.update(_parse_env_file(env_file))
        env.update(os.environ)

        def _req(*keys: str) -> str:
            for k in keys:
                if env.get(k):
                    return env[k]
            from alicecore.exceptions import ConfigError

            raise ConfigError(
                f"from_env is missing the required environment variable {' / '.join(keys)}; see .env.example for the minimal set."
            )

        def _opt(*keys: str) -> str | None:
            for k in keys:
                if env.get(k):
                    return env[k]
            return None

        def _int(*keys: str) -> int | None:
            v = _opt(*keys)
            return int(v) if v is not None else None

        def _float(*keys: str) -> float | None:
            v = _opt(*keys)
            return float(v) if v is not None else None

        # -- LLM / Embedding (standard names) --
        llm_kwargs: dict[str, object] = {
            "api_key": _req("OPENAI_API_KEY"),
            "model": _req("LLM_MODEL"),
            "base_url": _opt("OPENAI_BASE_URL"),
        }
        if (t := _float("LLM_TEMPERATURE")) is not None:
            llm_kwargs["temperature"] = t
        if (mt := _int("LLM_MAX_TOKENS")) is not None:
            llm_kwargs["max_tokens"] = mt
        if (to := _int("LLM_TIMEOUT")) is not None:
            llm_kwargs["timeout"] = to
        llm = LLMConfig(**llm_kwargs)  # type: ignore[arg-type]

        embedding = EmbeddingConfig(
            model=_req("EMBEDDING_MODEL"),
            dimensions=_int("EMBEDDING_DIMENSIONS"),
            api_key=_opt("EMBEDDING_API_KEY"),  # None -> the validator falls back to OPENAI_API_KEY
            base_url=_opt("EMBEDDING_BASE_URL"),
        )

        rerank = None
        if env.get("RERANK_MODEL"):
            rerank = RerankConfig(
                model=env["RERANK_MODEL"],
                api_key=_opt("RERANK_API_KEY"),
                base_url=_opt("RERANK_BASE_URL"),
            )

        # -- Storage choice --
        vector_provider = (_opt("VECTOR_PROVIDER") or "lancedb").lower()
        rel_provider = (_opt("RELATIONAL_PROVIDER") or "sqlite").lower()
        data_dir = _opt("DATA_DIR") or "./.alicecore"

        relational: RelationalConfig | None = None
        if rel_provider != "sqlite":  # sqlite is left to _derive_relational, which uses data_dir
            relational = RelationalConfig(
                provider=rel_provider,  # type: ignore[arg-type]
                host=_opt("DB_HOST") or "localhost",
                port=_int("DB_PORT")
                or (5432 if rel_provider in ("postgres", "postgresql") else 3306),
                user=_opt("DB_USER") or "sag2",
                password=_opt("DB_PASSWORD") or "sag2",
                database=_opt("DB_NAME") or "sag2",
            )

        es = ESConfig()
        if env.get("ES_HOSTS"):
            es = ESConfig(
                hosts=_parse_hosts(env["ES_HOSTS"]),
                username=_opt("ES_USERNAME"),
                password=_opt("ES_PASSWORD"),
            )

        return cls(
            relational=relational,
            es=es,
            vector_provider=vector_provider,  # type: ignore[arg-type]
            data_dir=data_dir,
            llm=llm,
            embedding=embedding,
            rerank=rerank,
            language=(_opt("LANGUAGE") or "en"),  # type: ignore[arg-type]
            log_level=(_opt("LOG_LEVEL") or "INFO"),  # type: ignore[arg-type]
        )


def _parse_env_file(path: str) -> dict[str, str]:
    """Minimal .env parsing: ``KEY=VALUE`` lines, skipping blanks and ``#`` comments, stripping wrapping quotes (no new dependency)."""
    from pathlib import Path

    out: dict[str, str] = {}
    text = Path(path).read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def _parse_hosts(value: str) -> list[str]:
    """ES_HOSTS parsing: a JSON array, a comma-separated list or a single URL all work."""
    value = value.strip()
    if value.startswith("["):
        import json

        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(h) for h in parsed]
        except ValueError:
            pass
    return [h.strip() for h in value.split(",") if h.strip()]
