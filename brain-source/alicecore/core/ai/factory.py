"""
LLM client factory

Creates the right LLM client from configuration, with per-scenario support
"""

import hashlib
import json
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from alicecore.core.ai.routing import RoutingLLMClient

from alicecore.core.ai.base import BaseLLMClient, LLMRetryClient
from alicecore.core.ai.models import ModelConfig, LLMProvider
from alicecore.core.ai.llm import OpenAIClient
from alicecore.core.config import get_settings
from alicecore.exceptions import ConfigError
from alicecore.utils import get_logger

logger = get_logger("ai.factory")

# Global client singletons
_embedding_client = None
_embedding_config_fingerprint: Optional[str] = None


def _get_client_fingerprint(config: Dict[str, Any]) -> str:
    """
    Build a client configuration fingerprint (shared helper)

    Only the core parameters that affect the client instance are included:
    - model: model name
    - api_key: API key
    - base_url: API address

    Other parameters (temperature, dimensions, timeout and so on) do not affect the client instance itself

    Args:
        config: configuration dictionary

    Returns:
        The configuration fingerprint (MD5 hash)
    """
    key_params = {
        "model": config.get("model"),
        "api_key": config.get("api_key"),
        "base_url": config.get("base_url"),
    }
    # Hash the configuration
    config_str = json.dumps(key_params, sort_keys=True)
    return hashlib.md5(config_str.encode()).hexdigest()


async def _load_db_config(type: str = "llm", scenario: str = "general") -> Optional[Dict[str, Any]]:
    """
    Load the model configuration from the database (shared helper) - disabled, environment variables are used

    Fallback strategy (for the LLM):
    1. look up the dedicated configuration for type + scenario
    2. fall back to type + 'general'
    3. return None (environment variables take over)

    For embedding and similar:
    - look up type + scenario directly (usually general)

    Args:
        type: model type (llm/embedding)
        scenario: usage scenario

    Returns:
        A configuration dictionary, or None
    """
    # Environment variable configuration is used by default; nothing is loaded from the database any more
    logger.debug(f"Using environment variable configuration: type={type}, scenario={scenario}")
    return None




def _build_single_client(spec: Dict[str, Any], defaults: Any) -> BaseLLMClient:
    """Build one provider description (dict) into an underlying client."""
    model_config_obj = ModelConfig(
        provider=LLMProvider.OPENAI,  # a single OPENAI protocol object; the branch below picks the real implementation
        model=spec["model"],
        api_key=spec["api_key"],
        base_url=spec.get("base_url"),
        temperature=spec.get("temperature") if spec.get("temperature") is not None else defaults.llm_temperature,
        max_tokens=spec.get("max_tokens") if spec.get("max_tokens") is not None else defaults.llm_max_tokens,
        top_p=defaults.llm_top_p,
        frequency_penalty=defaults.llm_frequency_penalty,
        presence_penalty=defaults.llm_presence_penalty,
        timeout=spec.get("timeout") if spec.get("timeout") is not None else defaults.llm_timeout,
        max_retries=spec.get("max_retries") if spec.get("max_retries") is not None else defaults.llm_max_retries,
        extra_body=spec.get("extra_body"),
    )
    if str(spec.get("provider") or defaults.llm_provider or "openai").lower() == "litellm":
        from alicecore.core.ai.litellm_client import LiteLLMClient

        return LiteLLMClient(model_config_obj)
    return OpenAIClient(model_config_obj)


def _build_routing_client(chain_spec: Any, *, scenario: str, defaults: Any) -> "RoutingLLMClient":
    """Assemble the routing chain by priority; an entry missing api_key/model raises instead of being skipped silently."""
    from alicecore.core.ai.routing import RoutedProvider, RoutingLLMClient

    entries = [dict(item) for item in chain_spec if item.get("enabled", True)]
    if not entries:
        raise ConfigError("No enabled provider in the LLM routing chain (check providers[].enabled)")

    invalid = [item.get("id") or "<no id>" for item in entries if not item.get("api_key") or not item.get("model")]
    if invalid:
        raise ConfigError(f"LLM provider is missing api_key or model: {', '.join(invalid)}")

    entries.sort(key=lambda item: item.get("priority", 100))
    routed = [
        RoutedProvider(
            id=str(item.get("id") or f"provider-{index}"),
            label=str(item.get("label") or f"{item.get('id')} / {item.get('model')}"),
            client=_build_single_client(item, defaults),
            max_retries=int(
                item["max_retries"] if item.get("max_retries") is not None else defaults.llm_max_retries
            ),
            cooldown_seconds=float(item.get("cooldown_seconds", 60.0)),
        )
        for index, item in enumerate(entries)
    ]
    logger.info(
        "Created a multi-provider routing client: scenario=%s, chain=%s",
        scenario,
        " → ".join(p.label for p in routed),
    )
    return RoutingLLMClient(routed)


async def create_llm_client(
    scenario: str = "general",
    model_config: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> "BaseLLMClient | LLMRetryClient | RoutingLLMClient":
    """
    Create an LLM client (the single entry point, with per-scenario support)

    Configuration priority (highest first):
    1. model_config passed in explicitly
    2. environment variable configuration (fallback)

    Args:
        scenario: scenario identifier, default 'general'
            - 'extract' : event extraction
            - 'search'  : search
            - 'chat'    : conversation
            - 'summary' : summarisation
            - 'system'  : system (agent creation and so on)
            - 'general' : general purpose (default)

        model_config: LLM configuration dictionary (optional)
            {
                'model': 'gpt-4',
                'api_key': 'sk-xxx',
                'base_url': 'https://<gateway-cua-ban>/v1',
                'temperature': 0.7,
                'max_tokens': 8000,
                ...
            }
            - when passed: used directly (highest priority)
            - when omitted: fetched from the configuration manager automatically

        **kwargs: loose parameters (backwards compatibility)

    Returns:
        An LLM client instance

    Raises:
        ConfigError: raised when no valid configuration can be obtained

    Examples:
        # Form 1: pass only the scenario and let it fetch the configuration (recommended)
        >>> client = await create_llm_client(scenario='extract')

        # Form 2: pass the configuration explicitly
        >>> client = await create_llm_client(
        ...     scenario='extract',
        ...     model_config={'model': 'gpt-4', 'temperature': 0.1}
        ... )

        # Form 3: use the default general scenario
        >>> client = await create_llm_client()

    Notes:
    - Dùng chung OpenAIClient (tương thích mọi endpoint OpenAI-compatible)
    - different vendors are told apart by base_url
    """
    settings = get_settings()

    # ============ Multi-provider routing (priority chain) ============
    # A non-empty providers list means routing: try them in priority order, and fall through to the next on 429 or an auth failure.
    chain_spec = (model_config or {}).get("providers") or settings.llm_providers
    if chain_spec:
        return _build_routing_client(chain_spec, scenario=scenario, defaults=settings)

    # ============ Configuration merge (three priority layers) ============

    # Layer 3: environment variable fallback
    config = {
        "model": settings.llm_model,
        "api_key": settings.llm_api_key,
        "base_url": settings.llm_base_url,
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
        "top_p": settings.llm_top_p,
        "frequency_penalty": settings.llm_frequency_penalty,
        "presence_penalty": settings.llm_presence_penalty,
        "timeout": settings.llm_timeout,
        "max_retries": settings.llm_max_retries,
    }

    # Layer 2: database configuration has been removed; environment variables are used directly

    # Layer 1: explicit configuration (highest priority)
    if model_config:
        config.update(model_config)
        logger.debug(f"Using the explicit configuration: scenario={scenario}")

    # Loose parameters (backwards compatibility)
    if kwargs:
        config.update(kwargs)

    # ============ Validate the required parameters ============
    if not config.get("api_key"):
        raise ConfigError(
            f"LLM configuration error: the API key is missing.\n"
            f"Scenario: {scenario}\n"
            f"Check the LLM_API_KEY environment variable"
        )

    if not config.get("model"):
        raise ConfigError(f"LLM configuration error: the model name is missing. Scenario: {scenario}")

    # ============ Build the configuration object ============
    model_config_obj = ModelConfig(
        provider=LLMProvider.OPENAI,  # always OPENAI (compatible with every gateway service)
        model=config["model"],
        api_key=config["api_key"],
        base_url=config.get("base_url"),
        temperature=config["temperature"],
        max_tokens=config["max_tokens"],
        top_p=config["top_p"],
        frequency_penalty=config["frequency_penalty"],
        presence_penalty=config["presence_penalty"],
        timeout=config["timeout"],
        max_retries=config["max_retries"],
    )

    # ============ Create the client (implementation chosen by provider) ============
    # Mặc định 'openai' (tương thích mọi endpoint OpenAI-compatible); 'litellm' là tuỳ chọn đa provider.
    provider_name = str(config.get("provider") or get_settings().llm_provider or "openai").lower()
    if provider_name == "litellm":
        from alicecore.core.ai.litellm_client import LiteLLMClient

        base_client: BaseLLMClient = LiteLLMClient(model_config_obj)
    else:
        base_client = OpenAIClient(model_config_obj)

    # Wrap it with the retry mechanism
    with_retry = config.get("with_retry", True)
    if with_retry:
        logger.debug(
            f"Created an LLM client (with retries): scenario={scenario}",
            extra={
                "scenario": scenario,
                "model": config["model"],
                "base_url": config.get("base_url") or "OpenAI official",
                "max_retries": config["max_retries"],
            },
        )
        return LLMRetryClient(base_client)

    logger.debug(
        f"Created an LLM client: scenario={scenario}",
        extra={
            "scenario": scenario,
            "model": config["model"],
        },
    )
    return base_client


# ============================================================
# Notes:
# - LLM client: a new instance every time, managed by each module itself (extractor, searcher and so on)
# - Embedding client: a global singleton, replaced automatically when the configuration changes
# ============================================================


# ============ Embedding client factory ============


async def create_embedding_client(
    scenario: str = "general",
    embedding_config: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> "EmbeddingClient":
    """
    Create an embedding client (the single entry point, with layered configuration)

    Configuration priority (highest first):
    1. embedding_config passed in explicitly
    2. database configuration (if USE_DB_CONFIG=true, model_type='embedding')
    3. environment variable configuration (fallback)

    Args:
        scenario: usage scenario, default 'general' (embedding currently only uses general; this may grow)
        embedding_config: embedding configuration dictionary (optional)
            {
                'model': 'Qwen/Qwen3-Embedding-0.6B',
                'api_key': 'sk-xxx',
                'base_url': 'https://<gateway-cua-ban>/v1',
                'dimensions': 1536,
                ...
            }
        **kwargs: loose parameters (backwards compatibility)

    Returns:
        An EmbeddingClient instance

    Raises:
        ConfigError: raised when no valid configuration can be obtained

    Examples:
        # Form 1: fetch the configuration automatically (recommended)
        >>> client = await create_embedding_client()

        # Form 2: pass the configuration explicitly
        >>> client = await create_embedding_client(
        ...     embedding_config={'model': 'text-embedding-3-large'}
        ... )
    """
    settings = get_settings()

    # ============ Configuration merge (three priority layers) ============

    # Layer 3: environment variable fallback
    config = {
        "model": settings.embedding_model_name,
        "api_key": settings.embedding_api_key or settings.llm_api_key,
        "base_url": settings.embedding_base_url or settings.llm_base_url,
        "dimensions": settings.embedding_dimensions,
        "timeout": 60,
        "max_retries": settings.embedding_max_retries,
    }

    # Layer 2: database configuration (with type='embedding')
    if settings.use_db_config:
        db_config = await _load_db_config(type="embedding", scenario=scenario)
        if db_config:
            # Extract dimensions (it may live inside extra_data)
            if "extra_data" in db_config and db_config["extra_data"]:
                if "dimensions" in db_config["extra_data"]:
                    db_config["dimensions"] = db_config["extra_data"]["dimensions"]
            config.update(db_config)
            logger.info(f"Using the database embedding configuration: model={db_config.get('model')}")
        else:
            logger.debug("No embedding configuration in the database, using environment variables")

    # Layer 1: explicit configuration (highest priority)
    if embedding_config:
        config.update(embedding_config)
    logger.info("Using the explicit embedding configuration")

    # Loose parameters (backwards compatibility)
    if kwargs:
        config.update(kwargs)

    # ============ Validate the required parameters ============
    if not config.get("api_key"):
        raise ConfigError(
            "Embedding configuration error: the API key is missing.\n"
            f"Scenario: {scenario}\n"
            "Check the database configuration, or the EMBEDDING_API_KEY/LLM_API_KEY environment variable"
        )

    if not config.get("model"):
        raise ConfigError(f"Embedding configuration error: the model name is missing. Scenario: {scenario}")

    # ============ Create the client ============
    from alicecore.core.ai.embedding import EmbeddingClient

    # Pull the parameters out and create the client (api_key included, so the database configuration takes effect)
    client = EmbeddingClient(
        model=config["model"],
        base_url=config.get("base_url"),
        api_key=config.get("api_key"),
        dimensions=config.get("dimensions"),
        max_retries=config.get("max_retries"),
    )

    logger.info(
        "Created an embedding client",
        extra={
            "scenario": scenario,
            "model": config["model"],
            "base_url": config.get("base_url") or "OpenAI official",
            "dimensions": config.get("dimensions") or "default",
        },
    )
    return client


# Global embedding client singleton (replaced automatically when the configuration changes)
_embedding_client: Optional["EmbeddingClient"] = None
_embedding_config_fingerprint: Optional[str] = None


async def get_embedding_client(scenario: str = "general") -> "EmbeddingClient":
    """
    Get the embedding client (singleton, configuration refreshes automatically)

    How it works:
    - one global instance is kept
    - every call checks whether the configuration changed (by fingerprint)
    - when it changed, the instance is replaced
    - when it did not, the existing instance is reused

    Fingerprint parameters: model, api_key, base_url (the usual three)

    Args:
        scenario: usage scenario, default 'general'

    Returns:
        An EmbeddingClient instance
    """
    global _embedding_client, _embedding_config_fingerprint

    # 1. Get the full configuration (environment variables, database configuration and so on merged)
    settings = get_settings()
    config = {
        "model": settings.embedding_model_name,
        "api_key": settings.embedding_api_key or settings.llm_api_key,
        "base_url": settings.embedding_base_url or settings.llm_base_url,
        "dimensions": settings.embedding_dimensions,
        "timeout": 60,
        "max_retries": settings.embedding_max_retries,
    }

    # 2. Try to load the configuration from the database
    if settings.use_db_config:
        db_config = await _load_db_config(type="embedding", scenario=scenario)
        if db_config:
            # Extract dimensions (it may live inside extra_data)
            if "extra_data" in db_config and db_config["extra_data"]:
                if "dimensions" in db_config["extra_data"]:
                    db_config["dimensions"] = db_config["extra_data"]["dimensions"]
            config.update(db_config)
            logger.debug(f"Using the database embedding configuration: model={db_config.get('model')}")

    # 3. Build the configuration fingerprint (from the key parameters: model, api_key, base_url)
    current_fingerprint = _get_client_fingerprint(config)

    # 4. Check whether the configuration changed
    if _embedding_client is None or current_fingerprint != _embedding_config_fingerprint:
        # Configuration changed, or this is the first creation
        action = "Updated" if _embedding_client else "Created"

        from alicecore.core.ai.embedding import EmbeddingClient

        _embedding_client = EmbeddingClient(
            model=config["model"],
            base_url=config.get("base_url"),
            api_key=config.get("api_key"),
            dimensions=config.get("dimensions"),
            max_retries=config.get("max_retries"),
        )
        _embedding_config_fingerprint = current_fingerprint

        logger.info(
            f"{action} the embedding client: model={config['model']}, "
            f"base_url={config.get('base_url') or 'default'}, "
            f"fingerprint={current_fingerprint[:8]}..."
        )
    else:
        logger.debug(f"Reusing the embedding client (configuration unchanged): {config['model']}")

    return _embedding_client


def reset_embedding_client() -> None:
    """Reset the embedding client singleton"""
    global _embedding_client, _embedding_config_fingerprint
    _embedding_client = None
    _embedding_config_fingerprint = None
    logger.info("Embedding client reset")


async def close_all_clients() -> None:
    """Close every global client and release the resources"""
    global _embedding_client, _embedding_config_fingerprint

    if _embedding_client:
        try:
            # EmbeddingClient may not have a close method, so check first
            if hasattr(_embedding_client, 'close'):
                await _embedding_client.close()
            logger.info("Embedding client closed")
        except Exception as e:
            logger.warning(f"Error while closing the embedding client: {e}")
        finally:
            _embedding_client = None
            _embedding_config_fingerprint = None
