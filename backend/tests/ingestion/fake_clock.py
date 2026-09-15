"""A fake monotonic clock for rate-limiter tests, so no test sleeps for real."""

import threading

from app.ingestion.rate_limit import RateLimiter


class FakeClock:
    """Time that moves only when a limiter sleeps on it.

    `frozen=True` keeps time still, so concurrent callers all reserve against
    the same instant and their queue positions become deterministic.
    """

    def __init__(self, frozen: bool = False) -> None:
        self.now = 0.0
        self.frozen = frozen
        self.sleeps: list[float] = []
        self._lock = threading.Lock()

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            if not self.frozen:
                self.now += seconds

    def limiter(self, min_interval_seconds: float = 0.0) -> RateLimiter:
        return RateLimiter(min_interval_seconds, clock=self.clock, sleep=self.sleep)
