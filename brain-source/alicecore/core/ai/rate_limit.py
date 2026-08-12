"""Đọc `Retry-After` của server và giữ một circuit breaker cho từng endpoint.

Vì sao cần: khi gateway trả `429 Retry-After: 690`, nó đã nói rõ "đừng gọi lại trong 690 giây".
Client chỉ biết backoff mũ (tối đa 30 giây) sẽ bắn tiếp ngay khi chưa hết ban → mỗi lần bắn lại
làm ban gia hạn → tự nhốt mình vĩnh viễn. Ingest chạy nhiều chunk song song còn nhân số lần bắn
lên gấp `concurrency` lần.

Hai cơ chế ở đây:

1. `retry_after_seconds()` — bóc thời gian chờ mà server yêu cầu, từ header `Retry-After`
   (delta-seconds hoặc HTTP-date) và từ phần text của thông báo lỗi cho gateway không đặt header.
2. `CircuitBreaker` — trạng thái **dùng chung cả process** theo từng endpoint. Đang mở thì mọi
   lời gọi hỏng ngay lập tức thay vì xếp hàng chờ; nhờ đó một lần 429 dài không biến thành hàng
   trăm request nữa và tài liệu FAILED sớm với lý do đọc được (thay vì treo im lặng).

Breaker **không** giấu lỗi: mở breaker luôn kèm lý do và số giây còn lại trong exception.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional

from alicecore.utils import get_logger

logger = get_logger("ai.rate_limit")

#: Trần khi server nói một con số vô lý (hoặc HTTP-date lệch đồng hồ). 1 giờ là đủ cho mọi
#: quota theo phút/giờ; dài hơn thì chờ cũng không còn ý nghĩa với một job ingest.
MAX_RETRY_AFTER_SECONDS = 3600.0

#: "try again in 690 seconds", "retry after 11 minutes", "please wait 30s"
_TEXT_PATTERNS = (
    re.compile(r"retry[- ]after[\"'\s:=]+(\d+(?:\.\d+)?)\s*(ms|s|sec|secs|second|seconds|m|min|minute|minutes)?", re.IGNORECASE),
    re.compile(r"(?:try|retry)\s+again\s+in\s+(\d+(?:\.\d+)?)\s*(ms|s|sec|secs|second|seconds|m|min|minute|minutes)?", re.IGNORECASE),
    re.compile(r"(?:please\s+)?wait\s+(\d+(?:\.\d+)?)\s*(ms|s|sec|secs|second|seconds|m|min|minute|minutes)", re.IGNORECASE),
)

_UNIT_SCALE = {
    "ms": 0.001,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
}


def _clamp(seconds: float) -> Optional[float]:
    if seconds <= 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def _from_header_value(raw: Any) -> Optional[float]:
    """`Retry-After` cho phép hai dạng: delta-seconds và HTTP-date (RFC 9110)."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return _clamp(float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    import datetime as _dt

    now = _dt.datetime.now(tz=when.tzinfo) if when.tzinfo else _dt.datetime.now()
    return _clamp((when - now).total_seconds())


def _headers_of(error: BaseException) -> Optional[Any]:
    """Header của response, cho openai SDK (`error.response.headers`) và httpx."""
    for holder in (error, getattr(error, "response", None)):
        headers = getattr(holder, "headers", None)
        if headers is not None:
            return headers
    return None


def retry_after_seconds(error: BaseException) -> Optional[float]:
    """Server yêu cầu chờ bao lâu, hoặc None nếu không nói.

    Ưu tiên header thật; chỉ khi không có header mới đọc text, vì text là phỏng đoán.
    """
    headers = _headers_of(error)
    if headers is not None:
        for name in ("retry-after", "Retry-After", "x-ratelimit-reset-after", "ratelimit-reset"):
            try:
                raw = headers.get(name)
            except Exception:  # noqa: BLE001 - header container lạ thì bỏ qua, không được nổ ở đây
                raw = None
            value = _from_header_value(raw)
            if value is not None:
                return value

    text = str(error)
    for pattern in _TEXT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        try:
            amount = float(match.group(1))
        except (TypeError, ValueError):
            continue
        unit = (match.group(2) or "s").lower()
        value = _clamp(amount * _UNIT_SCALE.get(unit, 1.0))
        if value is not None:
            return value
    return None


class CircuitOpenError(Exception):
    """Endpoint đang bị chặn — nêu rõ còn bao nhiêu giây và vì sao."""

    def __init__(self, endpoint: str, remaining: float, reason: str) -> None:
        self.endpoint = endpoint
        self.remaining = remaining
        self.reason = reason
        super().__init__(
            f"{endpoint} is not being called for another {remaining:.0f}s ({reason}). "
            "Calling it earlier only extends the ban."
        )


@dataclass
class _BreakerState:
    open_until: float = 0.0
    reason: str = ""
    consecutive_failures: int = 0
    opened_count: int = 0


@dataclass
class CircuitBreaker:
    """Trạng thái chặn theo endpoint, dùng chung cả process.

    Trạng thái là process-level có chủ đích: mỗi document ingest dựng client riêng, nhưng chúng
    gọi CÙNG một endpoint. Nếu breaker nằm trong instance thì 10 job song song vẫn bắn 10 lần
    vào một endpoint đang ban — đúng thứ đang phải sửa.
    """

    #: Chờ tại chỗ tối đa bấy nhiêu giây. Dài hơn thì mở breaker và hỏng ngay, vì giữ một job
    #: ingest ngồi đợi 11 phút không phải "kiên nhẫn", nó là treo không có tín hiệu.
    wait_in_place_max: float = 60.0
    #: Bao nhiêu lần hỏng liên tiếp (loại retryable, không có Retry-After) thì mở breaker.
    failure_threshold: int = 5
    #: Cửa sổ mở khi không có Retry-After: 30s, 60s, 120s… trần 300s.
    base_open_seconds: float = 30.0
    max_open_seconds: float = 300.0

    _states: Dict[str, _BreakerState] = field(default_factory=dict)

    def _state(self, endpoint: str) -> _BreakerState:
        state = self._states.get(endpoint)
        if state is None:
            state = _BreakerState()
            self._states[endpoint] = state
        return state

    def check(self, endpoint: str) -> None:
        """Raise `CircuitOpenError` nếu endpoint đang bị chặn."""
        state = self._state(endpoint)
        remaining = state.open_until - time.monotonic()
        if remaining > 0:
            raise CircuitOpenError(endpoint, remaining, state.reason or "circuit open")

    def remaining(self, endpoint: str) -> float:
        return max(0.0, self._state(endpoint).open_until - time.monotonic())

    def record_success(self, endpoint: str) -> None:
        state = self._state(endpoint)
        state.consecutive_failures = 0
        state.opened_count = 0
        state.open_until = 0.0
        state.reason = ""

    def record_failure(
        self,
        endpoint: str,
        error: BaseException,
        *,
        retry_after: Optional[float] = None,
    ) -> Optional[float]:
        """Ghi một lần hỏng.

        Returns:
            Số giây nên chờ **tại chỗ** rồi thử lại, hoặc None nếu không nên chờ tại chỗ
            (lúc đó breaker đã mở và lần `check()` kế tiếp sẽ hỏng ngay).
        """
        state = self._state(endpoint)
        state.consecutive_failures += 1

        if retry_after is None:
            retry_after = retry_after_seconds(error)

        if retry_after is not None:
            if retry_after <= self.wait_in_place_max:
                return retry_after
            self._open(endpoint, state, retry_after, f"server asked for Retry-After {retry_after:.0f}s")
            return None

        if state.consecutive_failures >= self.failure_threshold:
            window = min(
                self.base_open_seconds * (2 ** state.opened_count),
                self.max_open_seconds,
            )
            self._open(
                endpoint,
                state,
                window,
                f"{state.consecutive_failures} consecutive failures: {str(error)[:160]}",
            )
        return None

    def _open(self, endpoint: str, state: _BreakerState, seconds: float, reason: str) -> None:
        state.open_until = time.monotonic() + seconds
        state.reason = reason
        state.opened_count += 1
        state.consecutive_failures = 0
        logger.error(
            "Circuit opened for %s: no call for %.0fs (%s)",
            endpoint,
            seconds,
            reason,
        )

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Trạng thái hiện tại để tầng trên hiển thị/ghi log."""
        now = time.monotonic()
        return {
            endpoint: {
                "open_for": max(0.0, round(state.open_until - now, 1)),
                "reason": state.reason,
                "consecutive_failures": state.consecutive_failures,
            }
            for endpoint, state in self._states.items()
        }

    def reset(self) -> None:
        self._states.clear()


#: Breaker dùng chung cho embedding. Một endpoint embedding phục vụ mọi document của process này.
embedding_breaker = CircuitBreaker()
