"""Configuration bridging layer (internal).

Adapts the injected `EngineConfig` onto the internal `core` subsystem for `DataEngine`:
  1. `apply_config_to_env`  - writes the configuration into the process environment so the core `Settings` reads it (and clears its cache);
  2. `warmup_prompts`       - resolves the in-package prompts directory with `importlib.resources` and warms up the
     PromptManager singleton (so the core's `__file__` relative path does not break once installed from a wheel);
  3. `close_core_resources` - releases the db / es connections in one place.

This layer is an adapter; it does not modify `core` or any other internal logic.
"""

from __future__ import annotations

import json
import os
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from alicecore.config import EngineConfig


def _env_mapping(config: EngineConfig) -> dict[str, Any]:
    """Explicit mapping table from EngineConfig to the core Settings environment variables (entries whose value is None are skipped)."""
    es = urlsplit(config.es.hosts[0]) if config.es.hosts else urlsplit("")
    emb = config.embedding
    rk = config.rerank

    rel = config.relational
    if rel is None:  # already guaranteed by EngineConfig validation; this only narrows the type
        raise ValueError("the relational configuration is missing")

    mapping: dict[str, Any] = {
        # Relational database (the dialect comes from DB_PROVIDER; host and friends keep the MYSQL_* environment names)
        "DB_PROVIDER": rel.provider,
        "DB_PATH": rel.path,
        "MYSQL_HOST": rel.host,
        "MYSQL_PORT": rel.port,
        "MYSQL_USER": rel.user,
        "MYSQL_PASSWORD": rel.password,
        "MYSQL_DATABASE": rel.database,
        "DB_POOL_SIZE": rel.pool_size,
        "DB_MAX_OVERFLOW": rel.max_overflow,
        "DB_POOL_RECYCLE": rel.pool_recycle,
        # Vector store provider (lancedb | es | pgvector) and the local data root
        "VECTOR_PROVIDER": config.vector_provider,
        "DATA_DIR": config.data_dir,
        # Elasticsearch (scheme/host/port parsed from the first host)
        "ES_SCHEME": es.scheme or "http",
        "ES_HOST": es.hostname,
        "ES_PORT": es.port,
        "ES_USERNAME": config.es.username,
        "ELASTIC_PASSWORD": config.es.password,
        "ES_TIMEOUT": config.es.timeout,
        "ES_VERIFY_CERTS": str(config.es.verify_certs).lower(),
        # LLM; LLM_PROVIDERS is the **only** source of truth for the routing chain (a single provider folds into a one-element chain),
        # while the flat LLM_* variables remain only as a credential fallback for embedding/rerank and for backwards compatibility.
        "LLM_PROVIDERS": json.dumps(
            [p.model_dump() for p in config.llm.resolved_chain()],
            ensure_ascii=False,
        ),
        "LLM_PROVIDER": config.llm.provider,
        "LLM_API_KEY": config.llm.api_key,
        "LLM_MODEL": config.llm.model,
        "LLM_BASE_URL": config.llm.base_url,
        "LLM_TEMPERATURE": config.llm.temperature,
        "LLM_MAX_TOKENS": config.llm.max_tokens,
        "LLM_TIMEOUT": config.llm.timeout,
        "LLM_MAX_RETRIES": config.llm.max_retries,
        "LLM_ENABLE_THINK": str(config.llm.enable_think).lower(),
        # Embedding
        "EMBEDDING_MODEL_NAME": emb.model,
        "EMBEDDING_DIMENSIONS": emb.dimensions,
        "EMBEDDING_API_KEY": emb.api_key,
        "EMBEDDING_BASE_URL": emb.base_url,
        "EMBEDDING_MAX_RETRIES": emb.max_retries,
        # General
        "LLM_LANGUAGE": config.language,
        "LOG_LEVEL": config.log_level,
        "BENCHMARK": str(config.benchmark).lower(),
    }
    if rk is not None:
        mapping.update(
            {
                "RERANK_MODEL_NAME": rk.model,
                "RERANK_API_KEY": rk.api_key,
                "RERANK_BASE_URL": rk.base_url,
                "RERANK_ENDPOINT": rk.endpoint,
            }
        )
    return mapping


def apply_config_to_env(config: EngineConfig) -> None:
    """Write the configuration into os.environ and clear the lru_cache of the core Settings."""
    for key, value in _env_mapping(config).items():
        if value is not None:
            os.environ[key] = str(value)

    from alicecore.core.config import get_settings

    get_settings.cache_clear()


def ensure_local_dirs(config: EngineConfig) -> None:
    """Pre-create the directories a local backend needs (SQLite parent directory / LanceDB directory); SQLAlchemy does not create parents."""
    rel = config.relational
    if rel is not None and rel.provider == "sqlite" and rel.path:
        Path(rel.path).parent.mkdir(parents=True, exist_ok=True)
    if config.vector_provider == "lancedb":
        (Path(config.data_dir) / "lancedb").mkdir(parents=True, exist_ok=True)


def warmup_prompts() -> None:
    """Warm up the PromptManager singleton using the in-package resource path.

    The core `get_prompt_manager()` takes no arguments and derives its path from `__file__`, which points to the wrong
    place under a src-layout or a wheel; here the in-package prompts directory is located with `importlib.resources`
    and the singleton is instantiated before its first no-argument call, which then reuses it.
    """
    from alicecore.core.prompt import manager

    prompts_dir = Path(str(files("alicecore") / "prompts"))
    manager._prompt_manager = manager.PromptManager(prompts_dir=prompts_dir)


def reset_core_singletons() -> None:
    """Discard the process-level db engine / vector client singletons so a new configuration takes effect in start().

    Core connections are process-global singletons (the one-configuration-per-process limit of v0.x); resetting before
    start() guarantees this engine's configuration really lands (rather than reusing a stale connection), and also makes
    sequential multi-engine use and test isolation behave correctly.
    """
    from alicecore.core.storage.client import reset_es_client
    from alicecore.db.base import reset_engine

    reset_engine()
    reset_es_client()


async def close_core_resources() -> None:
    """Release the db / es connections in one place."""
    from alicecore.core.storage.client import close_es_client
    from alicecore.db.base import close_database

    await close_database()
    await close_es_client()
