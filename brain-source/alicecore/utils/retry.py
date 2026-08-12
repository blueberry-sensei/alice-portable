"""
Retry utilities

Provides exception classification and the retry logic
"""

import asyncio
from typing import Callable, Optional, Type, Tuple
from sqlalchemy.exc import OperationalError, IntegrityError

from alicecore.exceptions import RetryableError, NetworkError, ResourceBusyError, ServiceUnavailableError


def is_retryable_db_error(error: Exception) -> bool:
    """
    Decide whether a database error is retryable

    Args:
        error: the exception object

    Returns:
        True when retryable, False when not
    """
    if isinstance(error, OperationalError):
        error_str = str(error)
        # A deadlock or a lock wait timeout is retryable
        if "1213" in error_str or "Deadlock" in error_str:
            return True
        if "1205" in error_str or "Lock wait timeout" in error_str:
            return True
        # A lost connection is retryable
        if "2013" in error_str or "Lost connection" in error_str:
            return True
        # A connection timeout is retryable
        if "2003" in error_str or "Can't connect" in error_str:
            return True
        # Any other OperationalError is not retryable (a syntax error, for instance)
        return False

    # An IntegrityError (unique key conflict) is not retryable
    if isinstance(error, IntegrityError):
        return False

    return False


def is_retryable_network_error(error: Exception) -> bool:
    """
    Decide whether a network error is retryable

    Args:
        error: the exception object

    Returns:
        True when retryable, False when not
    """
    error_str = str(error).lower()

    # A connection timeout or a read timeout is retryable
    if "timeout" in error_str or "timed out" in error_str:
        return True

    # A refused or reset connection is retryable
    if "connection refused" in error_str or "connection reset" in error_str:
        return True

    # A temporary network error is retryable
    if "temporary failure" in error_str or "network unreachable" in error_str:
        return True

    return False


def is_retryable_error(error: Exception) -> bool:
    """
    Decide whether an exception is retryable (the single entry point)

    Args:
        error: the exception object

    Returns:
        True when retryable, False when not
    """
    # Custom retryable exceptions
    if isinstance(error, RetryableError):
        return True

    # Database errors
    if is_retryable_db_error(error):
        return True

    # Network errors
    if is_retryable_network_error(error):
        return True

    return False


async def retry_async(
    func: Callable,
    max_retries: int = 3,
    base_delay: float = 0.1,
    max_delay: float = 10.0,
    exponential_base: float = 2.0,
    retryable_exceptions: Optional[Tuple[Type[Exception], ...]] = None,
) -> any:
    """
    Async retry decorator

    Args:
        func: the async function
        max_retries: maximum retries
        base_delay: base delay (seconds)
        max_delay: maximum delay (seconds)
        exponential_base: exponential backoff base
        retryable_exceptions: retryable exception types (when None, is_retryable_error decides)

    Returns:
        The function result

    Raises:
        The exception of the last retry
    """
    last_exception = None

    for attempt in range(max_retries):
        try:
            return await func()
        except Exception as e:
            last_exception = e

            # Decide whether to retry
            if retryable_exceptions:
                is_retryable = isinstance(e, retryable_exceptions)
            else:
                is_retryable = is_retryable_error(e)

            # Not retryable, or the retry limit was reached
            if not is_retryable or attempt >= max_retries - 1:
                raise

            # Compute the delay (exponential backoff)
            delay = min(base_delay * (exponential_base ** attempt), max_delay)
            await asyncio.sleep(delay)

    # Unreachable in practice, but it keeps the type checker happy
    if last_exception:
        raise last_exception
