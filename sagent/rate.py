"""Rate limiting for sagent's LLM calls.

Two layers:

1. Proactive: a sliding-window limiter that caps calls per hour. Block (sleep)
   if we'd exceed the budget.
2. Reactive: detect rate-limit error signatures returned by the Anthropic
   API / Claude Agent SDK and raise a typed exception so the caller can
   back off without burning further calls.
"""

from __future__ import annotations

import time
from collections import deque


class SagentRateLimitError(RuntimeError):
    """Raised when the API tells us we've hit a quota wall."""


_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "rate-limit",
    "usage limit",
    "limit reached",
    "weekly limit",
    "5-hour",
    "5 hour",
    "throttle",
    "quota exceeded",
    "too many requests",
    "429",
)


def is_rate_limit_text(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(m in t for m in _RATE_LIMIT_MARKERS)


class RateLimiter:
    """Sliding-window N-per-hour limiter. Thread-unsafe (single-process use).

    `max_per_hour <= 0` disables the limiter entirely.
    """

    def __init__(self, max_per_hour: int = 0) -> None:
        self.max_per_hour = max_per_hour
        self._calls: deque[float] = deque()

    def acquire(self) -> None:
        if self.max_per_hour <= 0:
            return
        now = time.monotonic()
        cutoff = now - 3600.0
        while self._calls and self._calls[0] < cutoff:
            self._calls.popleft()
        if len(self._calls) >= self.max_per_hour:
            wait = self._calls[0] + 3600.0 - now
            if wait > 0:
                print(
                    f"[sagent] rate limit ({self.max_per_hour}/h reached): "
                    f"sleeping {wait:.0f}s"
                )
                time.sleep(wait)
            return self.acquire()
        self._calls.append(now)

    def record(self) -> None:
        """Record a call without blocking. Used after a successful call when
        acquire() was called elsewhere or skipped."""
        self._calls.append(time.monotonic())
