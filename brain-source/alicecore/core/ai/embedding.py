"""
Embedding generation service

Provides the shared text vectorisation capability used by every module
"""

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Optional, TypeVar

from alicecore.core.ai.rate_limit import (
    CircuitOpenError,
    embedding_breaker,
    retry_after_seconds,
)
from alicecore.core.config import get_settings
from alicecore.exceptions import AIError
from alicecore.utils import get_logger

logger = get_logger("ai.embedding")

T = TypeVar("T")


@dataclass
class EmbeddingUsage:
    """One embedding request, reported to the host so it can account for the cost.

    Embedding does not go through the LiteLLM boundary (it uses the openai SDK directly),
    so a host that only watches LiteLLM would miss it. This record closes that gap.
    """

    model: str
    base_url: Optional[str]
    #: How many texts were sent in this request (1 for `generate`, N for `batch_generate`).
    input_count: int
    prompt_tokens: int
    total_tokens: int
    latency_ms: int
    ok: bool
    error: Optional[str] = None


EmbeddingUsageCallback = Callable[[EmbeddingUsage], None]

#: Process-level sink for embedding usage. The engine does not care where the records go; it only
#: guarantees that **every** request, successful or failed, is reported exactly once.
_usage_sink: Optional[EmbeddingUsageCallback] = None


def set_embedding_usage_sink(sink: Optional[EmbeddingUsageCallback]) -> None:
    """Register the global embedding usage sink (pass None to clear it)."""
    global _usage_sink
    _usage_sink = sink


def get_embedding_usage_sink() -> Optional[EmbeddingUsageCallback]:
    return _usage_sink


def _report_usage(usage: EmbeddingUsage) -> None:
    sink = _usage_sink
    if sink is None:
        return
    try:
        sink(usage)
    except Exception as error:  # noqa: BLE001 - accounting must never break vectorisation
        logger.warning("Embedding usage sink failed: %s", error)


def _usage_tokens(response: Any) -> tuple[int, int]:
    """Read (prompt_tokens, total_tokens) from an embedding response; 0 when the endpoint omits usage."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0

    def _value(name: str) -> int:
        raw = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, 0)
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0

    prompt = _value("prompt_tokens")
    total = _value("total_tokens") or prompt
    return prompt, total

#: Embedding has no "try another provider" option - switching provider switches the vector space, and mixing two
#: coordinate systems in one index turns retrieval into noise. So this only retries **the same endpoint**; when the
#: retries run out it fails - never a downgrade, never silently.
_RETRYABLE_MARKERS = (
    "429",
    "rate limit",
    "too many requests",
    "quota",
    "resource exhausted",
    "overloaded",
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "connection error",
    "temporarily unavailable",
    "500",
    "502",
    "503",
    "504",
)


def _is_retryable(error: BaseException) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _RETRYABLE_MARKERS)


class EmbeddingClient:
    """
    Embedding client
    
    The unified text vectorisation service. Supports:
    - OpenAI Embedding API
    - a custom embedding service
    - a local embedding model (future extension)
    """
    
    def __init__(
        self,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        dimensions: Optional[int] = None,
        max_retries: Optional[int] = None,
        retry_delay: float = 2.0,
        backoff_factor: float = 2.0,
        max_delay: float = 30.0,
    ):
        """
        Initialise the embedding client

        Args:
            model: model name (read from configuration by default)
            base_url: API address (read from configuration by default)
            api_key: API key (read from configuration by default)
            dimensions: output vector dimensions (read from configuration by default)
            max_retries: maximum retries for retryable errors (read from configuration by default)
            retry_delay: first retry delay (seconds)
            backoff_factor: exponential backoff factor
            max_delay: cap for a single backoff (seconds)
        """
        from openai import AsyncOpenAI

        settings = get_settings()

        self.model = model or settings.embedding_model_name
        self.base_url = base_url or settings.embedding_base_url or settings.llm_base_url
        # Prefer the api_key that was passed in, then the environment variable
        self.api_key = api_key or settings.embedding_api_key or settings.llm_api_key
        self.dimensions = dimensions if dimensions is not None else settings.embedding_dimensions
        self.max_retries = (
            max_retries if max_retries is not None else getattr(settings, "embedding_max_retries", 3)
        )
        self.retry_delay = retry_delay
        self.backoff_factor = backoff_factor
        self.max_delay = max_delay

        # Initialise the OpenAI client.
        # max_retries=0: the SDK's built-in retry is **silent** (the log never shows how many retries happened), which
        # conflicts with "a failure must be visible". Retrying is handled by _with_retry, and every attempt is logged.
        client_kwargs: dict[str, Any] = {"api_key": self.api_key, "max_retries": 0}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url

        self.client = AsyncOpenAI(**client_kwargs)

        # Khoá của circuit breaker. Cùng endpoint + cùng model là cùng một hạn mức phía server,
        # nên mọi client dựng lên trong process này chia chung một trạng thái ban.
        self._breaker_key = f"{self.base_url or 'default'}::{self.model}"

        logger.info(
            f"Embedding client initialised",
            extra={
                "model": self.model,
                "base_url": self.base_url or "default",
                "dimensions": self.dimensions or "default",
                "max_retries": self.max_retries,
            },
        )

    async def _with_retry(self, operation: str, call: Callable[[], Awaitable[T]]) -> T:
        """Retry retryable errors with exponential backoff; a non-retryable error or exhausted retries -> raise AIError.

        Never returns an empty vector and never returns None - the caller gets either a valid vector or an exception.

        Hai luật thêm vào so với backoff thuần:

        - **Server nói chờ bao lâu thì chờ đúng bấy lâu.** `Retry-After` là con số duy nhất biết
          hạn mức thật; backoff mũ 2→4→8 giây chỉ đang đoán, và đoán ngắn hơn ban thì mỗi lần thử
          lại làm ban dài thêm.
        - **Ban dài thì hỏng ngay, không ngồi đợi.** Chờ tại chỗ 11 phút là treo không tín hiệu;
          mở circuit breaker rồi để document FAILED có lý do đọc được thì đúng hơn — và mọi chunk
          song song còn lại cũng dừng theo, thay vì mỗi chunk tự bắn thêm một request vào endpoint
          đang bị chặn.
        """
        last_error: Optional[BaseException] = None

        for attempt in range(self.max_retries + 1):
            try:
                embedding_breaker.check(self._breaker_key)
            except CircuitOpenError as blocked:
                logger.error("%s refused: %s", operation, blocked, extra={"model": self.model})
                raise AIError(f"{operation} refused: {blocked}") from blocked
            try:
                result = await call()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - classify first, then decide whether to retry
                last_error = error
                retryable = _is_retryable(error)
                if not retryable:
                    logger.error(
                        "%s failed (not retryable): %s",
                        operation,
                        error,
                        extra={"model": self.model, "attempt": attempt + 1},
                    )
                    raise AIError(f"{operation} failed (not retryable): {error}") from error

                asked = retry_after_seconds(error)
                wait = embedding_breaker.record_failure(self._breaker_key, error, retry_after=asked)
                if wait is None:
                    # Breaker đã mở (Retry-After quá dài, hoặc hỏng liên tiếp quá ngưỡng).
                    break
                if attempt >= self.max_retries:
                    break
                if asked is not None:
                    # Cộng thêm một chút để không bắn đúng biên rồi ăn 429 lần nữa.
                    delay = wait + 0.5 + random.random()
                else:
                    delay = min(
                        self.retry_delay * (self.backoff_factor**attempt) * (0.5 + random.random() * 0.5),
                        self.max_delay,
                    )
                logger.warning(
                    "%s failed, retrying in %.1fs (%d/%d)%s: %s",
                    operation,
                    delay,
                    attempt + 1,
                    self.max_retries,
                    " [server Retry-After]" if asked is not None else "",
                    error,
                    extra={"model": self.model},
                )
                await asyncio.sleep(delay)
            else:
                embedding_breaker.record_success(self._breaker_key)
                return result

        blocked_for = embedding_breaker.remaining(self._breaker_key)
        if blocked_for > 0:
            logger.error(
                "%s stopped: the endpoint is blocked for another %.0fs",
                operation,
                blocked_for,
                extra={"model": self.model, "error": str(last_error)},
            )
            raise AIError(
                f"{operation} stopped: {self.base_url or 'the embedding endpoint'} is blocked for "
                f"another {blocked_for:.0f}s (the server asked for it); last error: {last_error}"
            ) from last_error
        logger.error(
            "%s failed: still unsuccessful after %d retries",
            operation,
            self.max_retries,
            extra={"model": self.model, "error": str(last_error)},
        )
        raise AIError(f"{operation} failed: still unsuccessful after {self.max_retries} retries: {last_error}") from last_error

    async def _tracked(
        self,
        operation: str,
        input_count: int,
        call: Callable[[], Awaitable[T]],
        tokens: Callable[[], tuple[int, int]],
    ) -> T:
        """Run one embedding request through the retry policy and report its usage exactly once."""
        started = time.monotonic()
        try:
            result = await self._with_retry(operation, call)
        except Exception as error:  # noqa: BLE001 - report, then let the original exception through
            prompt_tokens, total_tokens = tokens()
            _report_usage(
                EmbeddingUsage(
                    model=self.model,
                    base_url=self.base_url,
                    input_count=input_count,
                    prompt_tokens=prompt_tokens,
                    total_tokens=total_tokens,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    ok=False,
                    error=str(error)[:500],
                )
            )
            raise
        prompt_tokens, total_tokens = tokens()
        _report_usage(
            EmbeddingUsage(
                model=self.model,
                base_url=self.base_url,
                input_count=input_count,
                prompt_tokens=prompt_tokens,
                total_tokens=total_tokens,
                latency_ms=int((time.monotonic() - started) * 1000),
                ok=True,
            )
        )
        return result

    async def generate(self, text: str) -> List[float]:
        """
        Generate the embedding vector of a text
        
        Args:
            text: the text content
            
        Returns:
            The embedding vector
            
        Raises:
            AIError: generation failed
        """
        request_kwargs: dict[str, Any] = {
            "input": text,
            "model": self.model,
        }
        if self.dimensions is not None:
            request_kwargs["dimensions"] = self.dimensions

        usage_tokens = (0, 0)

        async def _call() -> List[float]:
            nonlocal usage_tokens
            response = await self.client.embeddings.create(**request_kwargs)
            usage_tokens = _usage_tokens(response)
            if not response.data:
                raise AIError("the embedding endpoint returned empty data")
            return response.data[0].embedding

        embedding = await self._tracked("generate embedding", 1, _call, lambda: usage_tokens)
        logger.debug(
            f"Embedding generated",
            extra={
                "text_length": len(text),
                "vector_dim": len(embedding),
            },
        )
        return embedding


    async def batch_generate(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embedding vectors in batch
        
        Args:
            texts: list of texts
            
        Returns:
            List of embedding vectors
            
        Raises:
            AIError: generation failed
        """
        request_kwargs: dict[str, Any] = {
            "input": texts,
            "model": self.model,
        }
        if self.dimensions is not None:
            request_kwargs["dimensions"] = self.dimensions

        usage_tokens = (0, 0)

        async def _call() -> List[List[float]]:
            nonlocal usage_tokens
            response = await self.client.embeddings.create(**request_kwargs)
            usage_tokens = _usage_tokens(response)
            vectors = [item.embedding for item in response.data]
            if len(vectors) != len(texts):
                # A count mismatch means the vectors cannot be mapped back to their texts - reject at the source,
                # otherwise zip() silently drops the tail and the index gets vectors misaligned with their content.
                raise AIError(
                    f"embedding returned a mismatched count: expected {len(texts)}, got {len(vectors)}"
                )
            return vectors

        embeddings = await self._tracked(
            "batch generate embeddings", len(texts), _call, lambda: usage_tokens
        )
        logger.debug(
            f"Batch embeddings generated",
            extra={
                "batch_size": len(texts),
                "vector_dim": len(embeddings[0]) if embeddings else 0,
            },
        )
        return embeddings


# Global singleton
_embedding_client: Optional[EmbeddingClient] = None


def get_embedding_client() -> EmbeddingClient:
    """
    Get the global embedding client (singleton) - synchronous version
    
    Warning: this function is only for a purely synchronous environment (such as a test script).
    In an async environment use the async factory.get_embedding_client() instead.
    
    This function does not use the configuration manager; it reads configuration from environment variables only.
    
    Recommended:
    - factory.get_embedding_client(scenario='general') - async version, supports the configuration manager
    
    Returns:
        An EmbeddingClient instance
    """
    global _embedding_client
    if _embedding_client is None:
        # Simple construction, reading configuration from environment variables (no factory configuration management)
        _embedding_client = EmbeddingClient()
    return _embedding_client


def reset_embedding_client() -> None:
    """Reset the global embedding client"""
    global _embedding_client
    _embedding_client = None


async def generate_embedding(text: str) -> List[float]:
    """
    Convenience function for generating an embedding
    
    Args:
        text: the text content
        
    Returns:
        The embedding vector
    """
    client = get_embedding_client()
    return await client.generate(text)


async def batch_generate_embedding(texts: List[str]) -> List[List[float]]:
    """
    Convenience function for generating embeddings in batch
    
    Args:
        texts: list of texts
        
    Returns:
        List of embedding vectors
    """
    client = get_embedding_client()
    return await client.batch_generate(texts)
