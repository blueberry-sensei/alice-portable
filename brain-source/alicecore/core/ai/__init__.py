"""
AI module

Provides AI capabilities such as LLM calls and embedding generation
"""

from alicecore.core.ai.base import BaseLLMClient, LLMRetryClient
from alicecore.core.ai.embedding import (
    EmbeddingClient,
    EmbeddingUsage,
    batch_generate_embedding,
    generate_embedding,
    get_embedding_usage_sink,
    set_embedding_usage_sink,
)
from alicecore.core.ai.factory import (
    create_llm_client,
    create_embedding_client,
    get_embedding_client,
    reset_embedding_client,
)
from alicecore.core.ai.models import (
    ModelConfig,
    LLMMessage,
    LLMProvider,
    LLMResponse,
    LLMRole,
    LLMUsage,
)
from alicecore.core.ai.llm import OpenAIClient, create_openai_client

__all__ = [
    # Base
    "BaseLLMClient",
    "LLMRetryClient",
    # Models
    "ModelConfig",
    "LLMMessage",
    "LLMResponse",
    "LLMUsage",
    "LLMProvider",
    "LLMRole",
    # OpenAI
    "OpenAIClient",
    "create_openai_client",
    # Factory
    "create_llm_client",
    "create_embedding_client",
    "get_embedding_client",
    "reset_embedding_client",
    # Embedding
    "EmbeddingClient",
    "EmbeddingUsage",
    "generate_embedding",
    "batch_generate_embedding",
    "set_embedding_usage_sink",
    "get_embedding_usage_sink",
]
