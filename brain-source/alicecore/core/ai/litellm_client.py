"""litellm multi-provider LLM client (optional, mirrors cognee).

An optional implementation behind `create_llm_client`: enabled when ``LLM_PROVIDER=litellm``.
- chat / chat_stream go through **litellm** (OpenAI/Anthropic/local/any gateway, one entry point);
- chat_with_schema reuses the robust structured parsing of `BaseLLMClient` (json_repair + jsonschema),
  behaving exactly like the default OpenAIClient.

Needs the extra: ``pip install alicecore[litellm]``. The default provider is still openai, so not installing litellm changes nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, List, Optional

from alicecore.core.ai.base import BaseLLMClient
from alicecore.core.ai.models import LLMMessage, LLMResponse, LLMUsage
from alicecore.exceptions import ConfigError


def _load_litellm() -> Any:
    try:
        import litellm

        return litellm
    except ImportError as e:  # pragma: no cover - missing dependency path
        raise ConfigError(
            "Using the litellm provider needs the dependency: pip install 'alicecore[litellm]'"
        ) from e


class LiteLLMClient(BaseLLMClient):
    """LLM client backed by litellm."""

    def _model_name(self) -> str:
        # A custom OpenAI-compatible endpoint is routed with the openai/ prefix; anything already prefixed, or without a base_url, is left as is.
        model = self.config.model
        if self.config.base_url and "/" not in model:
            return f"openai/{model}"
        return model

    def _common_kwargs(
        self, temperature: Optional[float], max_tokens: Optional[int]
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._model_name(),
            "api_key": self.config.api_key,
            "api_base": self.config.base_url,
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": self.config.max_tokens if max_tokens is None else max_tokens,
            "timeout": self.config.timeout,
        }
        # Gateway-specific parameters (such as OpenRouter's provider.order picking a backend) are passed through untouched.
        if self.config.extra_body:
            kwargs["extra_body"] = dict(self.config.extra_body)
        return kwargs

    async def chat(
        self,
        messages: List[LLMMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        litellm = _load_litellm()
        resp = await litellm.acompletion(
            messages=[m.to_dict() for m in messages],
            **self._common_kwargs(temperature, max_tokens),
            **kwargs,
        )
        choice = resp.choices[0]
        content = getattr(choice.message, "content", None) or ""
        usage_obj = getattr(resp, "usage", None)
        usage = (
            LLMUsage(
                prompt_tokens=getattr(usage_obj, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage_obj, "completion_tokens", 0) or 0,
                total_tokens=getattr(usage_obj, "total_tokens", 0) or 0,
            )
            if usage_obj is not None
            else LLMUsage()
        )
        return LLMResponse(
            content=content,
            model=getattr(resp, "model", self.config.model),
            usage=usage,
            finish_reason=getattr(choice, "finish_reason", "stop") or "stop",
        )

    async def chat_stream(
        self,
        messages: List[LLMMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        include_reasoning: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[tuple[str, Optional[str]]]:
        litellm = _load_litellm()
        stream = await litellm.acompletion(
            messages=[m.to_dict() for m in messages],
            stream=True,
            **self._common_kwargs(temperature, max_tokens),
            **kwargs,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None) or ""
            reasoning = (
                getattr(delta, "reasoning_content", None) if include_reasoning else None
            )
            if content or reasoning:
                yield (content, reasoning)
