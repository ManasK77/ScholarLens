"""Process-wide request pacing for the external discovery APIs (arXiv,
Semantic Scholar).

Each source owns one `RateLimiter` for the life of the backend process. Every
request that source makes — first tries and retries alike, from any thread —
waits its turn on that limiter, so concurrent `/collect` runs queue behind each
other instead of each behaving as if it were the only client.

`send_with_retries` is the one bounded retry loop both sources use. A 429 or
5xx answer pushes the limiter's next free slot back for *every* caller, the
wait honours Retry-After and grows exponentially, and a wait the server
demands beyond the configured cap ends the attempt rather than being cut short.
"""

import email.utils
import logging
import math
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import TypeVar

logger = logging.getLogger(__name__)

# 429 is a rate limit; 5xx is transient overload. Anything else (a 403 for a
# bad key, a 400 for a bad query) will not fix itself, so it is not retried.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Upper bound on a remembered cooldown, so one absurd Retry-After cannot switch
# a source off until the process restarts.
MAX_COOLDOWN_SECONDS = 3600.0

ResponseT = TypeVar("ResponseT")


class CooldownActive(Exception):
    """Sending now would ignore a wait the source still requires."""

    def __init__(self, wait_seconds: float) -> None:
        self.wait_seconds = wait_seconds
        super().__init__(
            f"rate-limit cooldown active; next request allowed in {wait_seconds:.0f}s"
        )


class RateLimiter:
    """Minimum spacing between request starts, shared by every thread."""

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._lock = threading.Lock()
        self._min_interval = _non_negative(min_interval_seconds)
        self._next_slot: float | None = None
        self._clock = clock
        self._sleep = sleep

    @property
    def min_interval_seconds(self) -> float:
        return self._min_interval

    def set_min_interval(self, seconds: float) -> None:
        seconds = _non_negative(seconds)
        with self._lock:
            self._min_interval = seconds

    def acquire(self, max_wait_seconds: float | None = None) -> float:
        """Block until this caller may send one request; return the seconds waited.

        The slot is reserved under the lock and slept for outside it, so
        concurrent callers take consecutive slots. Raises `CooldownActive`, and
        reserves nothing, when the wait would exceed `max_wait_seconds`.
        """
        with self._lock:
            now = self._clock()
            start = now if self._next_slot is None else max(now, self._next_slot)
            wait = start - now
            if max_wait_seconds is not None and wait > max_wait_seconds:
                raise CooldownActive(wait)
            self._next_slot = start + self._min_interval
        if wait > 0:
            self._sleep(wait)
        return wait

    def defer(self, seconds: float) -> None:
        """Hold every caller back at least `seconds` from now; never shortens a wait."""
        seconds = min(max(seconds, 0.0), MAX_COOLDOWN_SECONDS)
        with self._lock:
            earliest = self._clock() + seconds
            if self._next_slot is None or self._next_slot < earliest:
                self._next_slot = earliest


def _non_negative(seconds: float) -> float:
    if seconds < 0:
        raise ValueError(f"a rate-limit interval cannot be negative, got {seconds}")
    return float(seconds)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def retry_after_seconds(
    headers: Mapping[str, str], now: Callable[[], datetime] = _utc_now
) -> float | None:
    """Seconds a `Retry-After` header asks for (delta-seconds or HTTP-date), or None."""
    value = headers.get("Retry-After")
    if value is None:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - now()).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(seconds, 0.0)


def retry_delay(headers: Mapping[str, str], attempt: int, base_seconds: float) -> float:
    """Wait before retry `attempt` (0-based): the longer of Retry-After and
    exponential backoff, so neither the server nor our own policy is undercut."""
    backoff = base_seconds * 2**attempt
    retry_after = retry_after_seconds(headers)
    return backoff if retry_after is None else max(retry_after, backoff)


class WaitBudgetExceeded(Exception):
    """The next slot is further away than the caller can wait; nothing was
    reserved and nothing was sent. Distinct from `CooldownActive`: the
    provider is not refusing, the caller simply has no time left."""

    def __init__(self, wait_seconds: float, budget_seconds: float) -> None:
        super().__init__(
            f"next request slot in {wait_seconds:.1f}s, "
            f"only {max(budget_seconds, 0.0):.1f}s left"
        )
        self.wait_seconds = wait_seconds
        self.budget_seconds = budget_seconds


def _acquire_within(
    limiter: RateLimiter,
    max_wait: float,
    wait_budget: Callable[[], float] | None,
) -> None:
    if wait_budget is None:
        limiter.acquire(max_wait_seconds=max_wait)
        return
    budget = wait_budget()
    if budget <= 0:
        raise WaitBudgetExceeded(0.0, budget)
    try:
        limiter.acquire(max_wait_seconds=min(max_wait, budget))
    except CooldownActive as exc:
        # Beyond the ordinary ceiling it is a real cooldown, reported as such.
        if exc.wait_seconds > max_wait:
            raise
        raise WaitBudgetExceeded(exc.wait_seconds, budget) from exc


def send_with_retries(
    send: Callable[[], ResponseT],
    *,
    limiter: RateLimiter,
    source: str,
    max_retries: int,
    backoff_base_seconds: float,
    backoff_max_seconds: float,
    retryable_exceptions: tuple[type[BaseException], ...] = (),
    retry_429_without_retry_after: bool = True,
    wait_budget: Callable[[], float] | None = None,
) -> ResponseT:
    """Send through `limiter`, retrying 429/5xx answers; return the final answer.

    Every attempt waits its turn on the shared limiter, and every retryable
    answer defers that limiter for all callers. Retrying stops once
    `max_retries` are spent or the required wait exceeds `backoff_max_seconds`;
    the last answer is returned for the caller to raise on. Logs name the source
    and the status only — never a URL or header, which can carry credentials.

    `retryable_exceptions` additionally retries a transport-level failure raised
    by `send()` itself (e.g. a read timeout or connection error) — there is no
    response in that case, so no Retry-After to honour, just the same
    exponential backoff. Defaults to `()`: a caller that doesn't pass it keeps
    the original status-code-only behaviour unchanged. Once `max_retries` is
    spent (or the required wait exceeds `backoff_max_seconds`), the exception is
    re-raised rather than swallowed — there is no "last answer" to return.

    `retry_429_without_retry_after=False` stops retrying a 429 that names no
    Retry-After (D-429-03): the server refused without saying when to come
    back, so a retry only guesses at a cooldown it cannot observe. The answer is
    returned after one attempt and the limiter is parked for the full
    `backoff_max_seconds`, which is longer than the exhausted-retry path would
    have deferred — fewer requests, not a shorter wait. A 429 that *does* carry
    Retry-After, and every 5xx, keep their retries. Defaults to True, so a
    caller that doesn't pass it is unchanged.

    `wait_budget`, when given, returns how many seconds the caller can still
    wait (Mode B: what is left of the query deadline). Before every attempt,
    the first and each retry, a slot further away than that raises
    `WaitBudgetExceeded`, reserving and sending nothing, so no request starts
    after the caller has stopped waiting for it. Defaults to `None`: no
    budget, the original behaviour.
    """
    # Queueing behind ordinary spacing is always allowed; only a longer,
    # server-imposed wait makes a caller give up without sending.
    max_wait = max(backoff_max_seconds, limiter.min_interval_seconds)
    attempt = 0
    while True:
        _acquire_within(limiter, max_wait, wait_budget)
        try:
            resp = send()
        except retryable_exceptions as exc:
            delay = min(backoff_base_seconds * 2**attempt, MAX_COOLDOWN_SECONDS)
            limiter.defer(delay)
            if attempt >= max_retries or delay > backoff_max_seconds:
                logger.warning(
                    "%s raised %s; giving up after %d attempt(s), "
                    "next request allowed in %.0fs",
                    source,
                    type(exc).__name__,
                    attempt + 1,
                    delay,
                )
                raise
            logger.warning(
                "%s raised %s; retry %d of %d in %.1fs",
                source,
                type(exc).__name__,
                attempt + 1,
                max_retries,
                delay,
            )
            attempt += 1
            continue
        if resp.status_code not in RETRYABLE_STATUS:
            return resp
        if (
            resp.status_code == 429
            and not retry_429_without_retry_after
            and retry_after_seconds(resp.headers) is None
        ):
            limiter.defer(backoff_max_seconds)
            logger.warning(
                "%s answered HTTP 429 with no Retry-After; not retrying this run, "
                "next request allowed in %.0fs",
                source,
                backoff_max_seconds,
            )
            return resp
        delay = retry_delay(resp.headers, attempt, backoff_base_seconds)
        limiter.defer(delay)
        if attempt >= max_retries or delay > backoff_max_seconds:
            logger.warning(
                "%s answered HTTP %d; giving up after %d attempt(s), "
                "next request allowed in %.0fs",
                source,
                resp.status_code,
                attempt + 1,
                delay,
            )
            return resp
        logger.warning(
            "%s answered HTTP %d; retry %d of %d in %.1fs",
            source,
            resp.status_code,
            attempt + 1,
            max_retries,
            delay,
        )
        _close(resp)
        attempt += 1


def _close(resp: object) -> None:
    close = getattr(resp, "close", None)
    if callable(close):
        close()
