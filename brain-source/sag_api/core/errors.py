"""sag domain exceptions - framework agnostic; the routing layer maps them to HTTP responses.

Domain services raise only these; the `sag/` adapter layer translates the `alicecore` `SagError` family into them.
"""

from __future__ import annotations


class ApiError(Exception):
    """Base class for every sag domain exception."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str | None = None, *, code: str | None = None):
        self.message = message or self.__class__.__doc__ or "Internal error"
        if code:
            self.code = code
        super().__init__(self.message)


class NotFoundError(ApiError):
    """The requested resource does not exist."""

    status_code = 404
    code = "not_found"


class ConflictError(ApiError):
    """Resource conflict (for example a duplicate creation)."""

    status_code = 409
    code = "conflict"


class ValidationError(ApiError):
    """Input validation failed."""

    status_code = 422
    code = "validation_error"


class AuthError(ApiError):
    """Not authenticated, or the credential is invalid."""

    status_code = 401
    code = "unauthorized"


class ForbiddenError(ApiError):
    """No permission to access this resource."""

    status_code = 403
    code = "forbidden"


class ConfigurationError(ApiError):
    """A required configuration is missing (for example no LLM configured)."""

    status_code = 400
    code = "configuration_error"


class UpstreamError(ApiError):
    """The upstream (LLM / engine) returned an error."""

    status_code = 502
    code = "upstream_error"


class ServiceUnavailableError(ApiError):
    """Temporarily unavailable (retryable, for example rate limiting / timeout)."""

    status_code = 503
    code = "service_unavailable"
