"""
Document processor

Generates the vectors
"""

from typing import List, Optional

from alicecore.utils import estimate_tokens, get_logger

logger = get_logger("modules.load.processor")


class DocumentProcessor:
    """Document processor, owns vector generation"""

    def __init__(
        self,
        llm_client=None,
        embedding_model_name: Optional[str] = None,
    ) -> None:
        """
        Initialise the document processor

        Args:
            llm_client: kept for compatibility, unused
            embedding_model_name: the vector model name (read from configuration when omitted)
        """
        from alicecore.core.config import get_settings

        settings = get_settings()

        # The model name passed in wins, otherwise it is read from the configuration
        self.embedding_model_name = embedding_model_name or settings.embedding_model_name
        logger.info(
            "Document processor initialised",
            extra={
                "embedding_model_name": self.embedding_model_name,
            },
        )

    async def generate_embedding(self, text: str) -> List[float]:
        """
        Generate the vector of a text

        Args:
            text: the text content

        Returns:
            The vector list

        Raises:
            AIError: vector generation failed
        """
        try:
            import time
            from alicecore.core.ai.factory import get_embedding_client
            from alicecore.exceptions import AIError
            from alicecore.utils import is_retryable_error

            total_start = time.perf_counter()

            truncate_start = time.perf_counter()
            truncated_text = self._truncate_content(text, max_tokens=8000)
            truncate_time = time.perf_counter() - truncate_start

            logger.debug(f"Generating a vector, text length: {len(text)} characters")

            api_start = time.perf_counter()
            embedding_client = await get_embedding_client(scenario='general')
            embedding = await embedding_client.generate(truncated_text)
            api_time = time.perf_counter() - api_start

            total_time = time.perf_counter() - total_start

            logger.info(
                f"Vector generation timing - "
                f"total: {total_time:.3f}s, "
                f"text truncation: {truncate_time:.3f}s ({truncate_time/total_time*100:.1f}%), "
                f"API call: {api_time:.3f}s ({api_time/total_time*100:.1f}%), "
                f"vector dimensions: {len(embedding)}"
            )

            return embedding

        except Exception as e:
            from alicecore.exceptions import AIError
            from alicecore.utils import is_retryable_error

            if is_retryable_error(e):
                logger.warning(f"Vector generation failed (retryable): {e}")
                raise AIError(f"Vector generation failed (temporary error): {e}") from e
            else:
                logger.error(f"Vector generation failed (not retryable): {e}")
                raise AIError(f"Vector generation failed (permanent error): {e}") from e

    def _truncate_content(self, content: str, max_tokens: int) -> str:
        """
        Truncate the content to fit the token limit

        Args:
            content: the raw content
            max_tokens: the maximum token count

        Returns:
            The truncated content
        """
        estimated_tokens = estimate_tokens(content)

        if estimated_tokens <= max_tokens:
            return content

        # Truncate proportionally, leaving 10% headroom
        ratio = max_tokens / estimated_tokens
        target_length = int(len(content) * ratio * 0.9)
        truncated = content[:target_length]

        logger.debug(
            f"Content truncated: {len(content)} characters -> {len(truncated)} characters "
            f"({estimated_tokens} tokens -> ~{max_tokens} tokens)"
        )

        return truncated
