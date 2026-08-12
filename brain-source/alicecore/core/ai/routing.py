"""Multi-provider routing and failover (by priority).

One call tries each provider in priority order; after a failure it decides by **category** instead of always retrying:

| Category        | Test                              | Action                                    |
|-----------------|-----------------------------------|---------------------------------------|
| `transient`     | timeout / connection reset / 5xx  | retry the same provider with backoff      |
| `rate_limit`    | 429 / quota / resource exhausted  | fall through immediately and cool it down - for exactly as long as the server's `Retry-After` says when it says anything |
| `auth`          | 401 / 403 / invalid api key       | mark unhealthy and fall through (a retry is pointless) |
| `model_missing` | 404 / model not found             | mark unhealthy and fall through           |
| `bad_request`   | 400 (none of the above)           | raise straight away (another provider fails the same way) |
| `unknown`       | everything else                   | fall through, but do not mark unhealthy   |

**Never silent**: every attempt (success or failure) is reported through the `on_attempt` callback so the host can persist or display it;
when every provider is exhausted, the `LLMError` message carries a summary of **each** provider's failure reason.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from alicecore.core.ai.base import BaseLLMClient
from alicecore.core.ai.models import LLMMessage, LLMResponse
from alicecore.core.ai.rate_limit import retry_after_seconds
from alicecore.exceptions import LLMError, LLMRateLimitError, LLMTimeoutError
from alicecore.utils import get_logger

logger = get_logger("ai.routing")

_STATUS_RE = re.compile(r"(?:error code|status(?:\s+code)?|http)\D{0,3}(\d{3})", re.IGNORECASE)


class FailureKind(str, Enum):
    """Failure category (decides retry versus fall through)."""

    TRANSIENT = "transient"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    MODEL_MISSING = "model_missing"
    BAD_REQUEST = "bad_request"
    UNKNOWN = "unknown"


#: These categories mean the provider's own configuration is wrong; retrying, or waiting, will not help.
UNHEALTHY_KINDS = frozenset({FailureKind.AUTH, FailureKind.MODEL_MISSING})


def _status_code(text: str) -> Optional[int]:
    match = _STATUS_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:  # pragma: no cover - the regex already guarantees three digits
        return None


def classify_failure(error: BaseException) -> FailureKind:
    """Classify an underlying exception into a routing category.

    Look at the exception type first (the client already wraps openai SDK exceptions into LLM* exceptions),
    then fall back to the status code and keywords in the message text - gateways do not agree on an error format.
    """
    if isinstance(error, LLMRateLimitError):
        return FailureKind.RATE_LIMIT
    if isinstance(error, LLMTimeoutError):
        return FailureKind.TRANSIENT

    text = str(error).lower()
    status = _status_code(text)

    if status == 429:
        return FailureKind.RATE_LIMIT
    if status in {401, 403}:
        return FailureKind.AUTH
    if status == 404:
        return FailureKind.MODEL_MISSING
    if status is not None and 500 <= status <= 599:
        return FailureKind.TRANSIENT

    if any(word in text for word in ("rate limit", "too many requests", "quota", "resource exhausted", "overloaded")):
        return FailureKind.RATE_LIMIT
    if any(
        word in text
        for word in ("invalid api key", "incorrect api key", "unauthorized", "unauthenticated", "permission denied")
    ):
        return FailureKind.AUTH
    if any(word in text for word in ("model not found", "no such model", "unknown model", "does not exist")):
        return FailureKind.MODEL_MISSING
    if any(word in text for word in ("timeout", "timed out", "connection reset", "connection refused", "connection error")):
        return FailureKind.TRANSIENT
    if status == 400:
        return FailureKind.BAD_REQUEST

    return FailureKind.UNKNOWN


@dataclass
class AttemptRecord:
    """The result of one provider attempt (successes are recorded too, so it is clear who actually answered)."""

    provider_id: str
    label: str
    model: str
    attempt: int
    ok: bool
    action: str  # ok | retry | failover | abort
    latency_ms: int
    kind: Optional[FailureKind] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "label": self.label,
            "model": self.model,
            "attempt": self.attempt,
            "ok": self.ok,
            "action": self.action,
            "latency_ms": self.latency_ms,
            "kind": self.kind.value if self.kind else None,
            "error": self.error,
        }


@dataclass
class RoutedProvider:
    """One provider in the routing chain."""

    id: str
    client: BaseLLMClient
    label: str = ""
    max_retries: int = 2
    cooldown_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not self.label:
            self.label = f"{self.id} / {self.client.config.model}"

    @property
    def model(self) -> str:
        return self.client.config.model


@dataclass
class _ProviderState:
    cooldown_until: float = 0.0
    #: Khác `cooldown_until` ở chỗ đây là con số **server tự nói** qua `Retry-After`. Cooldown
    #: đoán ra thì có thể bỏ qua khi cả chuỗi bận; ban do server đặt thì không — gọi sớm chỉ làm
    #: ban dài thêm, và đó chính là cách một provider bị nhốt vĩnh viễn.
    banned_until: float = 0.0
    unhealthy_reason: Optional[str] = None
    consecutive_failures: int = 0


AttemptCallback = Callable[[AttemptRecord], None]

#: Process-level sink for attempt records. A host application (such as the API layer) registers once and receives every routing
#: attempt for persistence or display; the engine itself does not care where they go, it only guarantees **every failure is reported**.
_attempt_sink: Optional[AttemptCallback] = None


def set_attempt_sink(sink: Optional[AttemptCallback]) -> None:
    """Register the global attempt-record sink (pass None to clear it)."""
    global _attempt_sink
    _attempt_sink = sink


def get_attempt_sink() -> Optional[AttemptCallback]:
    return _attempt_sink


class RoutingLLMClient:
    """LLM client that routes across several providers by priority.

    The interface matches `LLMRetryClient` (chat / chat_stream / chat_with_schema),
    so it is a drop-in replacement for the caller.
    """

    def __init__(
        self,
        providers: List[RoutedProvider],
        *,
        on_attempt: Optional[AttemptCallback] = None,
        retry_delay: float = 2.0,
        backoff_factor: float = 2.0,
        max_delay: float = 30.0,
    ) -> None:
        if not providers:
            raise LLMError("The routing chain is empty: at least one usable LLM provider is required")
        self.providers = providers
        self.on_attempt = on_attempt
        self.retry_delay = retry_delay
        self.backoff_factor = backoff_factor
        self.max_delay = max_delay
        self._states: Dict[str, _ProviderState] = {p.id: _ProviderState() for p in providers}

    # -- State ----------------------------------------------------------
    @property
    def config(self):  # noqa: ANN201 - compatibility for callers reading client.config.model
        return self.providers[0].client.config

    def health_snapshot(self) -> List[Dict[str, Any]]:
        """Current chain health, for the layer above (API/UI)."""
        now = time.monotonic()
        snapshot: List[Dict[str, Any]] = []
        for provider in self.providers:
            state = self._states[provider.id]
            snapshot.append(
                {
                    "provider_id": provider.id,
                    "label": provider.label,
                    "model": provider.model,
                    "unhealthy_reason": state.unhealthy_reason,
                    "cooldown_remaining": max(0.0, round(state.cooldown_until - now, 1)),
                    "banned_remaining": max(0.0, round(state.banned_until - now, 1)),
                    "consecutive_failures": state.consecutive_failures,
                }
            )
        return snapshot

    def _available(self) -> List[RoutedProvider]:
        now = time.monotonic()
        ready = [
            p
            for p in self.providers
            if self._states[p.id].unhealthy_reason is None and self._states[p.id].cooldown_until <= now
        ]
        if ready:
            return ready
        # When every provider is cooling down or unhealthy, still try the ones that are not unhealthy (a cooldown is advice, not a ban),
        # otherwise one rate limit makes the whole chain unusable for the length of the cooldown window.
        #
        # Ngoại lệ: provider đang trong `banned_until` thì KHÔNG được thử. Đó là con số server tự
        # đặt qua `Retry-After`; gọi sớm không phải "cố thêm một lần", nó gia hạn ban và biến sự
        # cố tạm thời thành vĩnh viễn.
        advisory = [
            p
            for p in self.providers
            if self._states[p.id].unhealthy_reason is None and self._states[p.id].banned_until <= now
        ]
        if advisory:
            return advisory
        return [p for p in self.providers if self._states[p.id].banned_until <= now]

    def _delay(self, attempt: int) -> float:
        base = self.retry_delay * (self.backoff_factor**attempt)
        jitter = 0.5 + random.random() * 0.5
        return min(base * jitter, self.max_delay)

    def _apply_rate_limit(self, provider: RoutedProvider, state: _ProviderState, error: BaseException) -> None:
        """429: nghỉ theo `Retry-After` của server nếu có, không thì theo cooldown cấu hình."""
        asked = retry_after_seconds(error)
        seconds = asked if asked is not None else provider.cooldown_seconds
        state.cooldown_until = time.monotonic() + seconds
        if asked is not None:
            state.banned_until = state.cooldown_until
            logger.warning(
                "Provider %s asked for Retry-After %.0fs - no call until then",
                provider.label,
                asked,
            )

    def _unavailable_detail(self) -> str:
        now = time.monotonic()
        parts = []
        for provider in self.providers:
            state = self._states[provider.id]
            if state.banned_until > now:
                parts.append(f"{provider.label} [banned {state.banned_until - now:.0f}s more]")
            elif state.unhealthy_reason:
                parts.append(f"{provider.label} [{state.unhealthy_reason}]")
        return "; ".join(parts) if parts else "no usable provider"

    def _report(self, record: AttemptRecord) -> None:
        sink = self.on_attempt or _attempt_sink
        if sink is None:
            return
        try:
            sink(record)
        except Exception as callback_error:  # noqa: BLE001 - the callback must not disturb the main flow
            logger.warning("Failed to report a provider attempt record: %s", callback_error)

    # -- Core: run the routing chain once --------------------------------
    async def _run(self, call: Callable[[BaseLLMClient], Any], *, what: str) -> Any:
        failures: List[str] = []
        last_error: Optional[BaseException] = None

        for provider in self._available():
            state = self._states[provider.id]
            for attempt in range(provider.max_retries + 1):
                started = time.monotonic()
                try:
                    result = await call(provider.client)
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - classify first, then decide the action
                    latency_ms = int((time.monotonic() - started) * 1000)
                    kind = classify_failure(error)
                    last_error = error
                    state.consecutive_failures += 1

                    retryable = kind is FailureKind.TRANSIENT and attempt < provider.max_retries
                    if kind is FailureKind.BAD_REQUEST:
                        action = "abort"
                    elif retryable:
                        action = "retry"
                    else:
                        action = "failover"

                    self._report(
                        AttemptRecord(
                            provider_id=provider.id,
                            label=provider.label,
                            model=provider.model,
                            attempt=attempt + 1,
                            ok=False,
                            action=action,
                            latency_ms=latency_ms,
                            kind=kind,
                            error=str(error)[:500],
                        )
                    )
                    logger.warning(
                        "%s failed [%s] provider=%s model=%s -> %s: %s",
                        what,
                        kind.value,
                        provider.label,
                        provider.model,
                        action,
                        error,
                    )

                    if action == "abort":
                        # The request itself is invalid, so another provider would only waste more quota.
                        raise LLMError(f"{what} failed (invalid request, no fall-through): {error}") from error

                    if action == "retry":
                        await asyncio.sleep(self._delay(attempt))
                        continue

                    if kind in UNHEALTHY_KINDS:
                        state.unhealthy_reason = f"{kind.value}: {str(error)[:200]}"
                    elif kind is FailureKind.RATE_LIMIT:
                        self._apply_rate_limit(provider, state, error)

                    failures.append(f"{provider.label} [{kind.value}] {str(error)[:200]}")
                    break
                else:
                    latency_ms = int((time.monotonic() - started) * 1000)
                    state.consecutive_failures = 0
                    state.cooldown_until = 0.0
                    state.banned_until = 0.0
                    self._report(
                        AttemptRecord(
                            provider_id=provider.id,
                            label=provider.label,
                            model=provider.model,
                            attempt=attempt + 1,
                            ok=True,
                            action="ok",
                            latency_ms=latency_ms,
                        )
                    )
                    return result

        detail = "; ".join(failures) if failures else self._unavailable_detail()
        raise LLMError(f"{what} failed: every provider is unavailable -> {detail}") from last_error

    # -- Public interface -------------------------------------------------
    async def chat(self, messages: List[LLMMessage], **kwargs: Any) -> LLMResponse:
        return await self._run(lambda client: client.chat(messages, **kwargs), what="LLM call")

    async def chat_with_schema(
        self,
        messages: List[LLMMessage],
        response_schema: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        return await self._run(
            lambda client: client.chat_with_schema(messages, response_schema, **kwargs),
            what="Structured output",
        )

    async def chat_stream(
        self,
        messages: List[LLMMessage],
        **kwargs: Any,
    ) -> AsyncIterator[tuple[str, Optional[str]]]:
        """Streaming call.

        **Falls through only before the first token**: content already handed to the caller cannot be taken back, and switching
        provider mid-stream would splice half a sentence from two models. After the first token a failure is raised, and the caller decides whether to start again.
        """
        failures: List[str] = []
        last_error: Optional[BaseException] = None

        for provider in self._available():
            state = self._states[provider.id]
            started = time.monotonic()
            emitted = False
            try:
                async for chunk in provider.client.chat_stream(messages, **kwargs):
                    if not emitted:
                        emitted = True
                        state.consecutive_failures = 0
                        state.cooldown_until = 0.0
                        state.banned_until = 0.0
                        self._report(
                            AttemptRecord(
                                provider_id=provider.id,
                                label=provider.label,
                                model=provider.model,
                                attempt=1,
                                ok=True,
                                action="ok",
                                latency_ms=int((time.monotonic() - started) * 1000),
                            )
                        )
                    yield chunk
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                kind = classify_failure(error)
                last_error = error
                state.consecutive_failures += 1
                action = "abort" if emitted else "failover"
                self._report(
                    AttemptRecord(
                        provider_id=provider.id,
                        label=provider.label,
                        model=provider.model,
                        attempt=1,
                        ok=False,
                        action=action,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        kind=kind,
                        error=str(error)[:500],
                    )
                )
                if emitted:
                    raise LLMError(f"Stream interrupted (content already produced, no fall-through): {error}") from error
                if kind in UNHEALTHY_KINDS:
                    state.unhealthy_reason = f"{kind.value}: {str(error)[:200]}"
                elif kind is FailureKind.RATE_LIMIT:
                    self._apply_rate_limit(provider, state, error)
                failures.append(f"{provider.label} [{kind.value}] {str(error)[:200]}")
                logger.warning("Streaming call failed [%s] provider=%s -> %s: %s", kind.value, provider.label, action, error)

        detail = "; ".join(failures) if failures else self._unavailable_detail()
        raise LLMError(f"Streaming call failed: every provider is unavailable -> {detail}") from last_error

    async def close(self) -> None:
        for provider in self.providers:
            try:
                await provider.client.close()
            except Exception as error:  # noqa: BLE001
                logger.warning("Failed to close provider %s: %s", provider.label, error)
