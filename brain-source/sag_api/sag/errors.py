"""Translate the alicecore `SagError` family into sag domain exceptions."""

from __future__ import annotations

from contextlib import contextmanager

from alicecore.exceptions import (
    ConfigError,
    InvalidInputError,
    NonRetryableError,
    ResourceNotFoundError,
    RetryableError,
    SagError,
)

from sag_api.core.errors import (
    ConfigurationError,
    NotFoundError,
    ServiceUnavailableError,
    UpstreamError,
    ValidationError,
)


@contextmanager
def map_sag_errors():
    """A SagError raised inside this context is translated into the matching ApiError."""
    try:
        yield
    except ConfigError as e:
        raise ConfigurationError(str(e)) from e
    except ResourceNotFoundError as e:
        raise NotFoundError(str(e)) from e
    except InvalidInputError as e:
        raise ValidationError(str(e)) from e
    except RetryableError as e:
        # Rate limiting / timeout / upstream temporarily down - retryable
        raise ServiceUnavailableError(str(e)) from e
    except NonRetryableError as e:
        raise ValidationError(str(e)) from e
    except SagError as e:
        raise UpstreamError(str(e)) from e
