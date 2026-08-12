"""
LLM base models

Defines the message, response and other data models of an LLM call
"""

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class LLMProvider(str, Enum):
    """LLM provider"""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    LOCAL = "local"


class LLMRole(str, Enum):
    """Message role"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class LLMMessage(BaseModel):
    """LLM message model"""

    role: LLMRole = Field(..., description="role")
    content: str = Field(..., description="message content")

    def to_dict(self) -> Dict[str, str]:
        """Convert to a dictionary"""
        # Pylint mistakes role for a FieldInfo; it is really an LLMRole enum
        return {"role": self.role.value, "content": self.content}  # pylint: disable=no-member


class LLMUsage(BaseModel):
    """Token usage statistics"""

    prompt_tokens: int = Field(default=0, description="input tokens")
    completion_tokens: int = Field(default=0, description="output tokens")
    total_tokens: int = Field(default=0, description="total tokens")


class LLMResponse(BaseModel):
    """LLM response model"""

    content: str = Field(..., description="response content")
    model: str = Field(..., description="model used")
    usage: LLMUsage = Field(default_factory=LLMUsage, description="token usage statistics")
    finish_reason: str = Field(default="stop", description="finish reason")

    @property
    def total_tokens(self) -> int:
        """Total tokens"""
        # Pylint mistakes usage for a FieldInfo; it is really an LLMUsage model
        return self.usage.total_tokens  # pylint: disable=no-member


class ModelConfig(BaseModel):
    """LLM configuration (behaviour parameters have no defaults; the factory injects them from settings + DB + explicit values)"""

    provider: LLMProvider = Field(..., description="provider")
    model: str = Field(..., description="model name")
    api_key: str = Field(..., description="API key")
    base_url: Optional[str] = Field(default=None, description="base URL")

    # Behaviour parameters (required, injected by the factory; defaults live in settings only)
    temperature: float = Field(..., ge=0.0, le=2.0, description="sampling temperature")
    max_tokens: int = Field(..., ge=1, description="maximum output tokens")
    top_p: float = Field(..., ge=0.0, le=1.0, description="top_p parameter")
    frequency_penalty: float = Field(..., ge=-2.0, le=2.0, description="frequency penalty")
    presence_penalty: float = Field(..., ge=-2.0, le=2.0, description="presence penalty")

    # Reliability parameters
    timeout: int = Field(default=600, ge=1, description="timeout in seconds")
    max_retries: int = Field(default=5, ge=0, description="maximum retries")

    # Extra request body passed through to the underlying API. Gateway-specific parameters go here, for example OpenRouter picking a backend:
    # {"provider": {"order": ["deepinfra/fp4"], "allow_fallbacks": false}}
    extra_body: Optional[Dict[str, Any]] = Field(default=None, description="extra request body (gateway-specific parameters)")
