"""
OpenAI LLM client implementation

Notes:
- supports standard OpenAI models (sophnet/Qwen3-30B-A3B-Thinking-2507, gpt-3.5-turbo and so on)
- supports thinking models: some models (such as Qwen3-30B-A3B-Thinking) put their reasoning in the
  reasoning_content field rather than content. This implementation detects and handles that automatically.
"""

from typing import Any, AsyncIterator, Iterable, List, Optional, cast

from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)
from openai.types.chat import ChatCompletionMessageParam

from alicecore.core.ai.base import BaseLLMClient
from alicecore.core.ai.models import ModelConfig, LLMMessage, LLMProvider, LLMResponse, LLMUsage
from alicecore.exceptions import LLMError, LLMRateLimitError, LLMTimeoutError
from alicecore.core.config.settings import get_settings 
from alicecore.utils import get_logger

logger = get_logger("ai.openai")


class OpenAIClient(BaseLLMClient):
    """OpenAI client implementation"""

    def __init__(self, config: ModelConfig) -> None:
        """
        Initialise the OpenAI client

        Args:
            config: LLM configuration
        """
        super().__init__(config)

        # Build the default headers (used to control content filtering and so on)
        default_headers = self._build_default_headers()

        # Create the AsyncOpenAI client
        self.client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout,
            default_headers=default_headers if default_headers else None,
        )

    def _extra_body(self) -> dict:
        """Extra fields in the request body.

        Only self-hosted vLLM / DashScope style endpoints understand `chat_template_kwargs.enable_thinking`;
        a public gateway returns 400 on an unknown field. Hence the rule:

        - `config.extra_body is None` -> keep the historical behaviour and send `chat_template_kwargs`;
        - `config.extra_body` is given (even `{}`) -> **the configuration decides**, use it to disable or replace.
        """
        if self.config.extra_body is not None:
            return dict(self.config.extra_body)
        settings = get_settings()
        return {"chat_template_kwargs": {"enable_thinking": settings.llm_enable_think}}

    def _build_default_headers(self) -> dict:
        """
        Build the default request headers

        Returns:
            The default request header dictionary
        """


        settings = get_settings()
        headers = {}

        # Add the DashScope header when content filtering is disabled
        if not settings.llm_data_inspection:
            headers["X-DashScope-DataInspection"] = '{"input": "disable", "output": "disable"}'

        return headers

    async def chat(
        self,
        messages: List[LLMMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """
        OpenAI chat completion

        Args:
            messages: list of messages
            temperature: sampling temperature
            max_tokens: maximum number of output tokens
            **kwargs: additional parameters

        Returns:
            The LLM response

        Raises:
            LLMError: the call failed
            LLMTimeoutError: the call timed out
            LLMRateLimitError: rate limited
        """
        try:
            # Prepare the messages
            api_messages = self._prepare_messages(messages)
            # Read the configuration
            settings = get_settings()
            # Log which model is being used
            logger.info(
                "Calling the LLM - model: %s, base_url: %s, temperature: %.2f, max_tokens: %s, timeout: %s, enable_think: %s",
                self.config.model,
                self.config.base_url,
                temperature or self.config.temperature,
                max_tokens or self.config.max_tokens or "unset",
                self.config.timeout,
                settings.llm_enable_think
            )

            # Log the message content (for debugging)
            logger.debug(
                "LLM request messages (%d): %s",
                len(messages),
                [
                    {
                        "role": m.role,
                        "content": (
                            m.content[:10000] + "..." if len(m.content) > 10000 else m.content
                        ),
                    }
                    for m in messages
                ],
            )

            # Call the API (with an explicit cast)
            response = await self.client.chat.completions.create(
                model=self.config.model,
                messages=cast(Iterable[ChatCompletionMessageParam], api_messages),
                temperature=temperature or self.config.temperature,
                max_tokens=max_tokens or self.config.max_tokens,  # read from configuration, never hard-coded
                extra_body=self._extra_body(),
                **kwargs,
            )

            # Parse the response
            choice = response.choices[0]
            usage = response.usage

            # Handle the response content
            content = choice.message.content
            reasoning = getattr(choice.message, "reasoning_content", None) or getattr(choice.message, "reasoning", None)  

            logger.debug(
                "OpenAI response: content=%s, reasoning_content=%s, finish_reason=%s",
                choice.message.content,
                reasoning,
                choice.finish_reason,
            )
            # Add the total token count
            logger.info(
                f"Token usage | prompt: {usage.prompt_tokens}, "
                f"completion: {usage.completion_tokens}, "
                f"total: {usage.prompt_tokens + usage.completion_tokens}"
            )


            return LLMResponse(
                content=content or "",
                model=response.model,
                usage=LLMUsage(
                    prompt_tokens=usage.prompt_tokens if usage else 0,
                    completion_tokens=usage.completion_tokens if usage else 0,
                    total_tokens=usage.total_tokens if usage else 0,
                ),
                finish_reason=choice.finish_reason or "stop",
            )

        except APITimeoutError as e:
            logger.error(
                "OpenAI call timed out - model: %s, base_url: %s, timeout: %s, error: %s",
                self.config.model,
                self.config.base_url,
                self.config.timeout,
                e,
            )
            raise LLMTimeoutError(f"OpenAI call timed out: {e}") from e
        except RateLimitError as e:
            logger.error(
                "OpenAI rate limited - model: %s, error: %s",
                self.config.model,
                e,
            )
            raise LLMRateLimitError(f"OpenAI rate limited: {e}") from e
        except (APIError, APIConnectionError) as e:
            logger.error(
                "OpenAI call failed - model: %s, base_url: %s, error: %s",
                self.config.model,
                self.config.base_url,
                e,
                exc_info=True,
            )
            raise LLMError(f"OpenAI call failed: {e}") from e
        except Exception as e:
            logger.error("Unknown error: %s", e, exc_info=True)
            raise LLMError(f"OpenAI call failed: {e}") from e

    async def chat_stream(
        self,
        messages: List[LLMMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        include_reasoning: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[tuple[str, Optional[str]]]:
        """
        OpenAI streaming chat completion

        Args:
            messages: list of messages
            temperature: sampling temperature
            max_tokens: maximum number of output tokens
            include_reasoning: whether to return the reasoning content (reasoning_content)
            **kwargs: additional parameters

        Yields:
            A tuple (content, reasoning) - content is a content fragment, reasoning is a reasoning fragment (when present)

        Raises:
            LLMError: the call failed
        """
        try:
            # Log which model is being used (max_tokens included)
            settings = get_settings()
            logger.info(
                "Calling the streaming LLM - model: %s, base_url: %s, temperature: %.2f, max_tokens: %s, timeout: %s, enable_think: %s",
                self.config.model,
                self.config.base_url,
                temperature or self.config.temperature,
                max_tokens or self.config.max_tokens or "unset",
                self.config.timeout,
                settings.llm_enable_think
            )

            # Print the input messages (for debugging)
            for i, msg in enumerate(messages):
                content_preview = msg.content[:5000] if len(msg.content) > 5000 else msg.content
                logger.info(f"Message[{i}] role={msg.role.value}: {content_preview}")

            # Prepare the messages
            api_messages = self._prepare_messages(messages)

            # Call the streaming API (with an explicit cast)
            stream = await self.client.chat.completions.create(
                model=self.config.model,
                messages=cast(Iterable[ChatCompletionMessageParam], api_messages),
                temperature=temperature or self.config.temperature,
                max_tokens=max_tokens or self.config.max_tokens,
                stream=True,
                extra_body=self._extra_body(),
                **kwargs,
            )

            # Yield the content fragments one by one
            async for chunk in stream:
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    content = delta.content if delta.content else None
                    reasoning = None

                    # When reasoning is wanted, try to read reasoning_content
                    if include_reasoning:
                        reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None) 

                    # Yield only when there is content or reasoning
                    if content or reasoning:
                        yield (content or "", reasoning)

        except APITimeoutError as e:
            logger.error("OpenAI streaming call timed out: %s", e)
            raise LLMTimeoutError(f"OpenAI streaming call timed out: {e}") from e
        except (APIError, APIConnectionError) as e:
            logger.error("OpenAI streaming call failed: %s", e, exc_info=True)
            raise LLMError(f"OpenAI streaming call failed: {e}") from e
        except Exception as e:
            logger.error("Unknown error: %s", e, exc_info=True)
            raise LLMError(f"OpenAI streaming call failed: {e}") from e

    async def close(self) -> None:
        """Close the OpenAI client and release the HTTP connections"""
        try:
            await self.client.close()
            logger.debug("OpenAI client closed")
        except Exception as e:
            logger.warning(f"Error while closing the OpenAI client: {e}")


async def create_openai_client(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    api_key: str,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: Optional[int] = None,
    max_retries: Optional[int] = None,
) -> OpenAIClient:
    """
    Create an OpenAI client (defaults read from environment variables)

    Args:
        api_key: API key
        model: model name (optional, read from environment variables by default)
        base_url: base URL (optional, read from environment variables by default)
        temperature: sampling temperature (optional, read from environment variables by default)
        max_tokens: maximum number of output tokens (optional, read from environment variables by default)
        timeout: timeout in seconds (optional, read from environment variables by default)
        max_retries: maximum number of retries (optional, read from environment variables by default)

    Returns:
        An OpenAI client instance
    """

    settings = get_settings()

    config = ModelConfig(
        provider=LLMProvider.OPENAI,
        model=model or settings.llm_model,
        api_key=api_key,
        base_url=base_url or settings.llm_base_url,
        temperature=temperature or settings.llm_temperature,
        max_tokens=max_tokens or settings.llm_max_tokens,
        top_p=settings.llm_top_p,
        frequency_penalty=settings.llm_frequency_penalty,
        presence_penalty=settings.llm_presence_penalty,
        timeout=timeout or settings.llm_timeout,
        max_retries=max_retries or settings.llm_max_retries,
    )

    return OpenAIClient(config)
