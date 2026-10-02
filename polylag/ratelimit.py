"""Client-side rate limiting and retry policy.

We stay well under the venue's published limits by choice. Getting rate-limited
during a fast market is the worst possible time to lose your data feed, and a
banned key is worse still. The bucket is shared by every caller of a host.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Awaitable, Callable, Optional, TypeVar

log = logging.getLogger("ratelimit")

T = TypeVar("T")


class TokenBucket:
    """Classic token bucket: `rate` tokens/second, up to `burst` saved up."""

    def __init__(self, rate: float, burst: int) -> None:
        if rate <= 0 or burst <= 0:
            raise ValueError("rate and burst must be positive")
        self.rate = float(rate)
        self.burst = float(burst)
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.burst, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                await asyncio.sleep((tokens - self._tokens) / self.rate)


class RetryPolicy:
    """Exponential backoff with full jitter, capped attempts."""

    def __init__(
        self,
        attempts: int = 3,
        base_delay: float = 0.4,
        max_delay: float = 8.0,
    ) -> None:
        self.attempts = attempts
        self.base_delay = base_delay
        self.max_delay = max_delay

    def delay_for(self, attempt: int, retry_after: Optional[float] = None) -> float:
        if retry_after is not None:
            return min(retry_after, self.max_delay)
        backoff = min(self.max_delay, self.base_delay * (2 ** attempt))
        return random.uniform(0, backoff)  # full jitter avoids thundering herds


class RetryableError(Exception):
    """Transport-level failure that is worth retrying (5xx, 429, timeout)."""

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


async def with_retries(
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    description: str,
) -> T:
    """Run `fn`, retrying only on RetryableError.

    Anything else (a 400 because our order was malformed, for example) is a bug
    on our side and must surface immediately rather than being hammered at the
    venue.
    """
    last: Optional[Exception] = None
    for attempt in range(policy.attempts):
        try:
            return await fn()
        except RetryableError as exc:
            last = exc
            if attempt == policy.attempts - 1:
                break
            delay = policy.delay_for(attempt, exc.retry_after)
            log.warning(
                "%s failed (%s); retry %d/%d in %.2fs",
                description, exc, attempt + 1, policy.attempts - 1, delay,
            )
            await asyncio.sleep(delay)
    raise RetryableError(f"{description} failed after {policy.attempts} attempts: {last}")
