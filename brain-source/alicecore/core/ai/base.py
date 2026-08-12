"""
LLM client base class

Defines the unified interface of an LLM client
"""

import asyncio
import random
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Dict, List, Optional

from alicecore.core.ai.models import ModelConfig, LLMMessage, LLMResponse, LLMRole
from alicecore.exceptions import LLMError, LLMTimeoutError
from alicecore.utils import get_logger

logger = get_logger("ai.llm")


#: Nhắc model chỉ trả JSON thuần. Engine KHÔNG dùng `response_format`/`json_schema`: nhiều
#: gateway OpenAI-compatible không hỗ trợ và trả lỗi mơ hồ ("Upstream request failed"), làm
#: chết cả job ingest. Prompt của caller đã mô tả schema, và phần bóc bên dưới (fence +
#: json_repair + unwrap) + kiểm schema + thử lại là đủ.
_JSON_ONLY_HINT = (
    "Return ONLY a raw JSON object matching the required schema. "
    "No prose, no explanation, no markdown fences."
)

class BaseLLMClient(ABC):
    """Base class for LLM clients"""

    def __init__(self, config: ModelConfig) -> None:
        """
        Initialise the LLM client

        Args:
            config: LLM configuration
        """
        self.config = config
        logger.info(
            "Initialising the %s client",
            config.provider.value,
            extra={"model": config.model},
        )

    @abstractmethod
    async def chat(
        self,
        messages: List[LLMMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """
        Chat completion

        Args:
            messages: list of messages
            temperature: sampling temperature
            max_tokens: maximum number of output tokens
            **kwargs: additional parameters

        Returns:
            The LLM response

        Raises:
            LLMError: the LLM call failed
            LLMTimeoutError: the call timed out
        """
        ...

    @abstractmethod
    def chat_stream(
        self,
        messages: List[LLMMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        include_reasoning: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[tuple[str, Optional[str]]]:
        """
        Streaming chat completion

        Args:
            messages: list of messages
            temperature: sampling temperature
            max_tokens: maximum number of output tokens
            include_reasoning: whether to return the reasoning content (reasoning_content)
            **kwargs: additional parameters

        Yields:
            A tuple (content, reasoning) - content is a content fragment, reasoning is a reasoning fragment (when present)

        Raises:
            LLMError: the LLM call failed
            LLMTimeoutError: the call timed out
        """
        ...

    async def chat_with_schema(
        self,
        messages: List[LLMMessage],
        response_schema: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Structured output (JSON Schema)

        Note: no prompt is injected automatically; the caller must state the output format inside messages.
        This method only: 1. calls the LLM  2. parses JSON  3. validates the schema (when one is given)

        Args:
            messages: list of messages (should contain the output format defined in SYSTEM)
            response_schema: JSON Schema definition (used for validation, optional)
            temperature: sampling temperature
            max_tokens: maximum number of output tokens
            **kwargs: additional parameters

        Returns:
            The parsed JSON object

        Raises:
            LLMError: the LLM call failed or the JSON was invalid
            ValidationError: the response does not match the schema (only when a schema is given)
        """
        import json

        api_kwargs = dict(kwargs)
        # Không gửi `response_format` — xem ghi chú ở _JSON_ONLY_HINT.
        messages = list(messages)
        if response_schema:
            messages.append(LLMMessage(role="user", content=_JSON_ONLY_HINT))

        response = await self.chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **api_kwargs,
        )

        # Parse the JSON response
        try:
            import re

            # Extract the JSON content (it may be wrapped in a markdown code block)
            content = response.content.strip()

            # Use a regular expression to extract a ```json or ``` code block
            json_block_match = re.search(
                r"```(?:json)?\s*\n(.*?)\n```",
                content,
                re.DOTALL | re.IGNORECASE,
            )
            if json_block_match:
                content = json_block_match.group(1).strip()
                logger.debug("Extracted JSON from a markdown code block")
            else:
                logger.debug("Parsing JSON directly (no code block)")

            # Parse: json.loads first, falling back to json_repair (trailing commas, stray text and so on)
            try:
                result = json.loads(content)
            except json.JSONDecodeError as parse_err:
                try:
                    import json_repair

                    result = json_repair.loads(content)
                    logger.info("The JSON returned by the LLM parsed successfully after json_repair")
                except Exception:
                    raise parse_err

            # Some models return a "JSON string wrapping a JSON object"; unwrap it once here.
            if isinstance(result, str):
                nested_content = result.strip()
                if nested_content.startswith(("{", "[")):
                    try:
                        result = json.loads(nested_content)
                    except json.JSONDecodeError:
                        try:
                            import json_repair

                            result = json_repair.loads(nested_content)
                            logger.info("The LLM returned a nested JSON string; the second parse succeeded")
                        except Exception:
                            pass

            # Validate when a schema was provided
            if response_schema:
                expected_type = response_schema.get("type")
                if expected_type == "object" and not isinstance(result, dict):
                    raise LLMError(
                        f"Response type does not match the schema: expected object, got {type(result).__name__}"
                    )
                if expected_type == "array" and not isinstance(result, list):
                    raise LLMError(
                        f"Response type does not match the schema: expected array, got {type(result).__name__}"
                    )

                # Try strict validation with jsonschema
                try:
                    import jsonschema

                    jsonschema.validate(instance=result, schema=response_schema)
                    logger.debug("JSON schema validation passed")
                except ImportError:
                    # jsonschema is not installed; fall back to a simple check (required fields, only when the root is a dict)
                    if isinstance(result, dict) and "properties" in response_schema:
                        required = response_schema.get("required", [])
                        for field in required:
                            if field not in result:
                                raise ValueError(f"Missing required field: {field}")
                    logger.debug("JSON simple validation passed")
                except Exception as e:
                    # jsonschema validation failed
                    if type(e).__name__ == "ValidationError":
                        logger.error(
                            "JSON schema validation failed: %s\nResponse content: %s",
                            e,
                            str(result)[:500],
                        )
                        raise LLMError(f"Response does not match the schema: {e}") from e
                    raise
            else:
                # No schema: only the JSON format is validated (already done by json.loads)
                logger.debug("JSON format validation passed (no schema provided)")

            return result

        except json.JSONDecodeError as e:
            logger.error("JSON parsing failed: %s\nContent: %s", e, response.content)
            raise LLMError(f"The LLM did not return valid JSON: {e}") from e
        except ValueError as e:
            logger.error("Schema validation failed: %s", e)
            raise LLMError(f"Response does not match the schema: {e}") from e

    def _prepare_messages(
        self,
        messages: List[LLMMessage],
    ) -> List[Dict[str, str]]:
        """
        Prepare the message list (convert it to the API format)

        Args:
            messages: list of messages

        Returns:
            The message list in API format
        """
        return [msg.to_dict() for msg in messages]

    async def close(self) -> None:
        """
        Close the client and release its resources

        Subclasses holding resources that need closing (such as HTTP connections) should override this
        """
        pass


class LLMRetryClient:
    """LLM client wrapper that adds a retry mechanism"""

    def __init__(
        self,
        client: BaseLLMClient,
        max_retries: Optional[int] = None,
        retry_delay: float = 4.0,
        backoff_factor: float = 2.0,
    ) -> None:
        """
        Initialise the retrying client

        Args:
            client: the underlying LLM client
            max_retries: maximum number of retries (None uses the client configuration)
            retry_delay: initial retry delay (seconds)
            backoff_factor: backoff factor
        """
        self.client = client
        self.max_retries = max_retries or client.config.max_retries
        self.retry_delay = retry_delay
        self.backoff_factor = backoff_factor

    def _should_retry(self, error: Exception) -> bool:
        """
        Decide whether an error should be retried

        Args:
            error: the exception object

        Returns:
            True to retry, False not to retry
        """
        # Timeout errors are not retried (a network problem is likely to time out again)
        if isinstance(error, LLMTimeoutError):
            return False

        # Rate limit errors should be retried
        from alicecore.exceptions import LLMRateLimitError

        if isinstance(error, LLMRateLimitError):
            return True

        # Other LLM errors may be retried
        if isinstance(error, LLMError):
            return True

        # Unknown errors are not retried by default
        return False

    def _compute_delay(self, attempt: int) -> float:
        """
        Compute the exponential backoff delay (with random jitter)

        delay = retry_delay × backoff_factor^attempt × (0.5 ~ 1.0 jitter)

        Example (retry_delay=4, backoff_factor=2):
          attempt 0: 4 × 1  × jitter = 2.0~4.0s
          attempt 1: 4 × 2  × jitter = 4.0~8.0s
          attempt 2: 4 × 4  × jitter = 8.0~16.0s
          attempt 3: 4 × 8  × jitter = 16.0~32.0s
          attempt 4: 4 × 16 × jitter = 32.0~64.0s
        """
        base_delay = self.retry_delay * (self.backoff_factor ** attempt)
        jitter = 0.5 + random.random() * 0.5
        return base_delay * jitter

    async def chat(
        self,
        messages: List[LLMMessage],
        **kwargs: Any,
    ) -> LLMResponse:
        """
        Chat completion with retries

        Implements exponential backoff (with random jitter so several workers do not retry in lockstep)

        The error type decides whether a retry happens:
        - timeout error: no retry
        - rate limit: retry
        - other LLM errors: retry
        """
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                return await self.client.chat(messages, **kwargs)
            except Exception as e:
                last_error = e

                # Decide whether to retry
                if not self._should_retry(e):
                    logger.error("Hit a non-retryable error: %s", e)
                    raise

                if attempt < self.max_retries:
                    delay = self._compute_delay(attempt)
                    logger.warning(
                        "LLM call failed, retrying in %.1fs (attempt %d/%d)",
                        delay,
                        attempt + 1,
                        self.max_retries,
                        extra={"error": str(e), "error_type": type(e).__name__},
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "LLM call failed after %d retries",
                        self.max_retries,
                        exc_info=True,
                    )

        raise LLMError(f"LLM call failed after {self.max_retries} retries") from last_error

    async def chat_stream(
        self,
        messages: List[LLMMessage],
        **kwargs: Any,
    ) -> AsyncIterator[tuple[str, Optional[str]]]:
        """
        Streaming call (no retries)

        A streaming call cannot be retried once it has failed, so the exception is raised directly

        Yields:
            A tuple (content, reasoning) - content is a content fragment, reasoning is a reasoning fragment (when present)
        """
        async for chunk in self.client.chat_stream(messages, **kwargs):
            yield chunk

    async def chat_with_schema(
        self,
        messages: List[LLMMessage],
        response_schema: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Structured output with retries

        The error type decides whether a retry happens
        """
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                return await self.client.chat_with_schema(
                    messages,
                    response_schema,
                    **kwargs,
                )
            except Exception as e:
                last_error = e

                # Decide whether to retry
                if not self._should_retry(e):
                    logger.error("Hit a non-retryable error: %s", e)
                    raise

                if attempt < self.max_retries:
                    logger.warning(
                        "Structured output failed, retrying immediately (attempt %d/%d)",
                        attempt + 1,
                        self.max_retries,
                        extra={"error": str(e), "error_type": type(e).__name__},
                    )

        raise LLMError(
            f"Structured output failed after {self.max_retries} retries, last error: {last_error}"
        ) from last_error
