from __future__ import annotations

import time

from sagent.rate import RateLimiter, SagentRateLimitError, is_rate_limit_text


def test_is_rate_limit_text_positive():
    assert is_rate_limit_text("Claude AI usage limit reached")
    assert is_rate_limit_text("Rate limit exceeded")
    assert is_rate_limit_text("rate_limit_error")
    assert is_rate_limit_text("HTTP 429")
    assert is_rate_limit_text("Weekly limit hit")
    assert is_rate_limit_text("5-hour window")


def test_is_rate_limit_text_negative():
    assert not is_rate_limit_text("server error")
    assert not is_rate_limit_text("connection refused")
    assert not is_rate_limit_text("")


def test_zero_disables_limiter():
    rl = RateLimiter(max_per_hour=0)
    for _ in range(100):
        rl.acquire()  # should never block


def test_acquire_records_calls():
    rl = RateLimiter(max_per_hour=10)
    rl.acquire()
    rl.acquire()
    assert len(rl._calls) == 2


def test_drops_old_entries(monkeypatch):
    rl = RateLimiter(max_per_hour=2)
    # simulate three calls with monotonic spaced > 1 hour apart
    times = iter([100.0, 4000.0, 4001.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(times))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    rl.acquire()  # at t=100
    rl.acquire()  # at t=4000 — old call dropped (> 1h ago)
    # would-be third call at t=4001 — should be allowed because the t=100
    # call is now stale and gets dropped at acquire time
    rl.acquire()


def test_blocks_when_full(monkeypatch):
    """When window is full, acquire sleeps then succeeds on retry."""
    rl = RateLimiter(max_per_hour=1)
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    # Use a fake clock that advances on each call so the recursive acquire
    # eventually finds an old-enough timestamp.
    clock = iter([0.0, 100.0, 4000.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    rl.acquire()  # t=0
    rl.acquire()  # t=100 — over budget; sleeps; recursive call at t=4000 succeeds
    assert sleeps and sleeps[0] > 0


def test_sagent_rate_limit_error_is_runtime_error():
    assert issubclass(SagentRateLimitError, RuntimeError)
