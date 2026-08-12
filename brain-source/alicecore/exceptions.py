"""
pipeline exception definitions

Every custom exception inherits from the SagError base class
"""


class SagError(Exception):
    """Base pipeline exception"""

    def __init__(self, message: str, *args: object) -> None:
        self.message = message
        super().__init__(message, *args)


class ConfigError(SagError):
    """Configuration error"""

    pass


class StorageError(SagError):
    """Storage layer error"""

    pass


class DatabaseError(StorageError):
    """Database error"""

    pass


class CacheError(StorageError):
    """Cache error"""

    pass


class LLMError(SagError):
    """LLM call error"""

    pass


class LLMTimeoutError(LLMError):
    """LLM call timeout"""

    pass


class LLMRateLimitError(LLMError):
    """LLM rate limit error"""

    pass


class AIError(SagError):
    """AI related error (covers both LLM and embedding)"""

    pass


class ValidationError(SagError):
    """Data validation error"""

    pass


class LoadError(SagError):
    """Document loading error"""

    pass


class EntityError(SagError):
    """Entity processing error"""

    pass


class ExtractError(SagError):
    """Event extraction error"""

    pass


class SearchError(SagError):
    """Retrieval error"""

    pass


class PromptError(SagError):
    """Prompt error"""

    pass


# ============ Retryable exceptions ============


class RetryableError(SagError):
    """Base class for retryable exceptions (a temporary error, a retry may succeed)"""

    pass


class NetworkError(RetryableError):
    """Network error (connection timeout, network interruption and so on)"""

    pass


class ResourceBusyError(RetryableError):
    """Resource busy error (database lock, concurrency conflict and so on)"""

    pass


class ServiceUnavailableError(RetryableError):
    """Service unavailable error (an external service is temporarily down)"""

    pass


# ============ Non-retryable exceptions ============


class NonRetryableError(SagError):
    """Base class for non-retryable exceptions (a permanent error, retrying is pointless)"""

    pass


class InvalidInputError(NonRetryableError):
    """Invalid input error (malformed data, illegal parameter and so on)"""

    pass


class ResourceNotFoundError(NonRetryableError):
    """Resource not found error (file missing, record missing and so on)"""

    pass


class PermissionError(NonRetryableError):
    """Permission error (access denied, authentication failed and so on)"""

    pass
