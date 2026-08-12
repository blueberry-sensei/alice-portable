"""
Configuration management module

Uses pydantic-settings, reading from environment variables and a .env file
"""

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_project_root() -> Path:
    """Find the project root (the directory containing .env)"""
    current = Path(__file__).resolve()

    # Walk upwards looking for a directory that contains .env
    for parent in [current.parent] + list(current.parents):
        env_file = parent / ".env"
        if env_file.exists():
            return parent

    # When nothing is found, return the project root of this file (assumed to be pipeline/core/config/)
    return current.parent.parent.parent


# LLM reliability constants (one default, so nothing is hard-coded in several places)
DEFAULT_LLM_MAX_RETRIES = 5


class Settings(BaseSettings):
    """Application settings"""

    model_config = SettingsConfigDict(
        env_file=str(_find_project_root() / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ======================
    # Database settings
    # ======================
    db_provider: str = Field(default="mysql", description="relational dialect: mysql | postgres | sqlite | oceanbase")
    db_path: str = Field(default="", description="SQLite file path (when db_provider=sqlite)")
    vector_provider: str = Field(
        default="lancedb",
        description=(
            "vector store: lancedb (default, local embedded) | es | pgvector (reuses postgres, needs db_provider=postgres)"
            " | oceanbase (reuses OceanBase, needs db_provider=oceanbase, OB V4.3.3+)"
        ),
    )
    data_dir: str = Field(default="./.alicecore", description="local data root (SQLite / LanceDB on disk)")
    lancedb_path: str = Field(default="", description="LanceDB directory (empty means data_dir/lancedb)")
    mysql_host: str = Field(default="localhost", description="relational host")
    mysql_port: int = Field(default=3306, description="relational port")
    mysql_user: str = Field(default="sag2", description="relational user")
    mysql_password: str = Field(default="sag2", description="relational password")
    mysql_database: str = Field(default="sag2", description="relational database name")

    # ======================
    # Elasticsearch settings
    # ======================
    es_host: str = Field(default="localhost", description="ES host")
    es_port: int = Field(default=9201, description="ES port")
    es_scheme: str = Field(default="http", description="ES scheme (http/https)")
    es_username: Optional[str] = Field(default="elastic", description="ES username")
    es_password: Optional[str] = Field(
        default=None, description="ES password", validation_alias="ELASTIC_PASSWORD"
    )

    # ======================
    # LLM settings (a gateway API or OpenAI official)
    # ======================
    llm_provider: str = Field(
        default="openai",
        description="LLM client implementation: openai (default, gateway compatible) | litellm (multi-provider, needs the [litellm] extra)",
    )
    llm_api_key: str = Field(default="", description="LLM API key")
    llm_model: str = Field(default="sophnet/Qwen3-30B-A3B-Thinking-2507", description="LLM model")
    llm_base_url: Optional[str] = Field(
        default=None, description="LLM API base URL (empty uses OpenAI official)"
    )
    llm_data_inspection: bool = Field(
        default=False, description="whether LLM content filtering is enabled, off by default"
    )

    # Whether the model's thinking mode (enable_thinking) is on; off by default, enable it explicitly in .env
    llm_enable_think: bool = Field(
        default=False, description="whether the model's thinking mode (enable_thinking) is on"
    )

    # LLM behaviour parameters: the defaults live only here; the global configuration = environment variable when present, otherwise this default; a database configuration may override it
    llm_temperature: float = Field(default=0.7, ge=0.0, le=2.0, description="LLM sampling temperature")
    llm_max_tokens: int = Field(default=30000, ge=1, description="LLM maximum output tokens")
    llm_top_p: float = Field(default=1.0, ge=0.0, le=1.0, description="LLM top_p parameter")
    llm_frequency_penalty: float = Field(default=0.0, ge=-2.0, le=2.0, description="frequency penalty")
    llm_presence_penalty: float = Field(default=0.0, ge=-2.0, le=2.0, description="presence penalty")

    # LLM reliability parameters
    llm_timeout: int = Field(default=300, ge=1, description="LLM timeout in seconds")
    llm_max_retries: int = Field(default=DEFAULT_LLM_MAX_RETRIES, ge=0, description="LLM maximum retries")

    # Priority routing chain (a JSON array string, injected by EngineConfig through _bootstrap).
    # When non-empty the factory builds a RoutingLLMClient: providers are tried by priority, falling through on 429 or an auth failure.
    # When empty it falls back to the flat single-provider configuration above.
    llm_providers: List[Dict[str, Any]] = Field(
        default_factory=list, description="LLM provider priority chain (multi-provider failover)"
    )

    @field_validator("llm_providers", mode="before")
    @classmethod
    def _parse_llm_providers(cls, v: Any) -> Any:
        """An environment variable can only be a string, so the JSON array is unpacked here; an empty string counts as \"not configured\"."""
        if isinstance(v, str):
            text = v.strip()
            if not text:
                return []
            import json

            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as e:
                raise ValueError(f"LLM_PROVIDERS is not valid JSON: {e}") from e
            if not isinstance(parsed, list):
                raise ValueError("LLM_PROVIDERS must be a JSON array")
            return parsed
        return v

    # Database configuration switch
    use_db_config: bool = Field(default=True, description="whether to use the database configuration")

    # ======================
    # Embedding settings (a gateway API or OpenAI official)
    # ======================
    embedding_api_key: str = Field(
        default="", description="embedding API key (empty uses llm_api_key)"
    )
    embedding_model_name: str = Field(
        default="Qwen/Qwen3-Embedding-0.6B", description="embedding model"
    )
    embedding_dimensions: Optional[int] = Field(
        default=None,
        description="embedding dimensions (optional; empty uses the model default - text-embedding-3-small defaults to 1536, text-embedding-3-large to 3072)",
    )
    embedding_base_url: Optional[str] = Field(
        default=None, description="embedding API base URL (empty uses llm_base_url)"
    )
    # Embedding does not fall through to another provider (a different model means a different vector space); it only retries the same endpoint;
    # when the retries run out it raises, the layer above marks that document failed, and a record with a missing vector is never written.
    embedding_max_retries: int = Field(
        default=3, ge=0, description="maximum retries for a retryable embedding error"
    )

    # ======================
    # LLM language settings
    # ======================
    # ======================
    # Rerank Configuration
    # ======================
    rerank_api_key: Optional[str] = Field(
        default=None, description="Rerank API key; fallback to embedding_api_key/llm_api_key when empty"
    )
    rerank_model_name: Optional[str] = Field(
        default=None, description="Rerank model name"
    )
    rerank_base_url: Optional[str] = Field(
        default=None, description="Rerank API base URL; fallback to embedding_base_url when empty"
    )
    rerank_endpoint: Optional[str] = Field(
        default="/rerank",
        description="rerank request endpoint path, appended after rerank_base_url (default /rerank; "
                    "set it to /reranks and so on when the platform routes there; "
                    "when rerank_base_url already contains the full endpoint path it is used as is, with nothing appended)",
    )

    llm_language: str = Field(
        default="en",
        description="Ngôn ngữ prompt trích xuất: 'en' (mặc định, schema ổn định "
                    "nhất) hoặc 'vi'. Nạp alicecore/prompts/<mã>/, thiếu thì bù "
                    "bằng bản gốc tiếng Anh. KHÔNG liên quan ngôn ngữ giao diện.",
    )

    # ======================
    # Application settings
    # ======================
    server_type: str = Field(
        default="LOCAL", description="service environment type (SAAS/LOCAL)"
    )
    benchmark: bool = Field(default=False, description="benchmark mode, skips the LLM call")
    debug: bool = Field(default=False, description="debug mode")
    log_level: str = Field(default="INFO", description="log level")
    log_format: str = Field(default="json", description="log format")

    # ======================
    # MLflow settings
    # ======================
    mlflow_port: int = Field(default=5000, description="MLflow Docker container port")
    mlflow_url: Optional[str] = Field(
        default="http://localhost:5000", description="MLflow tracking server address"
    )

    # Entity weight settings
    # entity_weights: str = Field(
    #     default="time:0.9,location:1.0,person:1.1,topic:1.5,action:1.2,tags:1.0",
    #     description="entity type weights",
    # )

    # ======================
    # Performance settings
    # ======================
    db_pool_size: int = Field(default=100, description="database connection pool size")
    db_max_overflow: int = Field(default=200, description="database connection pool maximum overflow")
    db_pool_recycle: int = Field(default=3600, description="database connection recycle time in seconds")

    # Cache TTLs
    cache_entity_ttl: int = Field(default=86400, description="entity cache TTL in seconds")
    cache_llm_ttl: int = Field(default=604800, description="LLM cache TTL in seconds")
    cache_search_ttl: int = Field(default=3600, description="search cache TTL in seconds")

    
    @property
    def database_url(self) -> str:
        """Build the async connection URL from db_provider (mysql/postgres/sqlite)."""
        from urllib.parse import quote_plus

        provider = (self.db_provider or "mysql").lower()
        if provider == "sqlite":
            import os

            path = self.db_path or os.path.join(self.data_dir, "sag.db")
            return f"sqlite+aiosqlite:///{path}"

        user = quote_plus(self.mysql_user)
        pwd = quote_plus(self.mysql_password)
        host, port, db = self.mysql_host, self.mysql_port, self.mysql_database
        if provider in ("postgres", "postgresql"):
            return f"postgresql+asyncpg://{user}:{pwd}@{host}:{port}/{db}"
        # mysql by default; OceanBase speaks the MySQL protocol, so it reuses the aiomysql driver and the mysql dialect
        return f"mysql+aiomysql://{user}:{pwd}@{host}:{port}/{db}?charset=utf8mb4"

    @property
    def lancedb_uri(self) -> str:
        """Local LanceDB directory (data_dir/lancedb unless set explicitly)."""
        import os

        return self.lancedb_path or os.path.join(self.data_dir, "lancedb")

    @property
    def mysql_url(self) -> str:
        """Relational connection URL (backwards compatible alias -> database_url)."""
        return self.database_url

    @property
    def elasticsearch_url(self) -> str:
        """Elasticsearch connection URL"""
        return f"{self.es_scheme}://{self.es_host}:{self.es_port}"

    @property
    def es_url(self) -> str:
        """Elasticsearch connection URL (legacy compatibility)"""
        return self.elasticsearch_url

    @property
    def amqp_url(self) -> str:
        """AMQP connection URL (RabbitMQ)"""
        from urllib.parse import quote_plus

        user = quote_plus(self.rabbitmq_username)
        pwd = quote_plus(self.rabbitmq_password)
        return f"amqp://{user}:{pwd}@{self.rabbitmq_host}:{self.rabbitmq_port}/{self.rabbitmq_vhost}"

    # @property
    # def entity_weights_dict(self) -> Dict[str, float]:
    #     """Entity weight dictionary"""
    #     result = {}
    #     for pair in self.entity_weights.split(","):
    #         if ":" in pair:
    #             key, value = pair.split(":")
    #             try:
    #                 result[key.strip()] = float(value.strip())
    #             except ValueError:
    #                 continue
    #     return result

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate the log level"""
        allowed = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in allowed:
            raise ValueError(f"The log level must be one of: {', '.join(allowed)}")
        return v.upper()

    @field_validator("llm_language")
    @classmethod
    def validate_llm_language(cls, v: str) -> str:
        """Giữ contract ngôn ngữ nội bộ khớp với ``EngineConfig`` công khai."""
        allowed = ["en", "vi"]
        if v.lower() not in allowed:
            raise ValueError(f"Ngôn ngữ LLM phải là: {', '.join(allowed)}")
        return v.lower()

    @field_validator("server_type")
    @classmethod
    def validate_server_type(cls, v: str) -> str:
        """Validate the service environment type"""
        normalized = v.upper()
        allowed = ["SAAS", "LOCAL"]
        if normalized not in allowed:
            raise ValueError(f"SERVER_TYPE must be one of: {', '.join(allowed)}")
        return normalized


@lru_cache()
def get_settings() -> Settings:
    """Get the settings singleton"""
    return Settings()
