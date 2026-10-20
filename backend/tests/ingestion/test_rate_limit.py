"""Tests for app.ingestion.rate_limit — the process-wide request pacing the
external discovery sources (arXiv, Semantic Scholar) share.

Everything runs on a fake clock except one wall-clock concurrency check.
"""

import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from app.ingestion import rate_limit
from app.ingestion.rate_limit import CooldownActive, RateLimiter
from tests.ingestion.fake_clock import FakeClock

# ---------------------------------------------------------------------------
# RateLimiter: spacing
# ---------------------------------------------------------------------------


def test_the_first_request_goes_out_immediately():
    fake = FakeClock()

    assert fake.limiter(3.0).acquire() == 0.0
    assert fake.sleeps == []


def test_consecutive_requests_are_spaced_by_the_minimum_interval():
    fake = FakeClock()
    limiter = fake.limiter(3.0)

    for _ in range(3):
        limiter.acquire()

    assert fake.sleeps == [3.0, 3.0]


def test_time_already_elapsed_counts_toward_the_interval():
    fake = FakeClock()
    limiter = fake.limiter(3.0)

    limiter.acquire()
    fake.now += 1.0
    limiter.acquire()  # 2s of the interval still to go
    fake.now += 10.0
    limiter.acquire()  # the interval has long passed

    assert fake.sleeps == [2.0]


def test_the_interval_can_be_reconfigured_but_not_made_negative():
    fake = FakeClock()
    limiter = fake.limiter(3.0)
    limiter.set_min_interval(5.0)

    limiter.acquire()
    limiter.acquire()

    assert fake.sleeps == [5.0]
    assert limiter.min_interval_seconds == 5.0
    with pytest.raises(ValueError):
        limiter.set_min_interval(-1.0)
    with pytest.raises(ValueError):
        RateLimiter(-1.0)


# ---------------------------------------------------------------------------
# RateLimiter: concurrency
# ---------------------------------------------------------------------------


def _run_concurrently(count: int, target) -> None:
    barrier = threading.Barrier(count)

    def _caller() -> None:
        barrier.wait()
        target()

    threads = [threading.Thread(target=_caller) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def test_concurrent_callers_share_one_queue():
    """Eight threads arriving at the same instant get eight distinct slots."""
    fake = FakeClock(frozen=True)
    limiter = fake.limiter(2.0)
    waits: list[float] = []
    lock = threading.Lock()

    def _acquire() -> None:
        waited = limiter.acquire()
        with lock:
            waits.append(waited)

    _run_concurrently(8, _acquire)

    assert sorted(waits) == [0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0]


def test_concurrent_callers_are_spaced_in_real_time():
    """The k-th caller to get through never starts before k intervals have passed."""
    interval = 0.1
    limiter = RateLimiter(interval)
    starts: list[float] = []
    lock = threading.Lock()

    def _acquire() -> None:
        limiter.acquire()
        stamp = time.monotonic()
        with lock:
            starts.append(stamp)

    begun = time.monotonic()
    _run_concurrently(4, _acquire)

    # Tolerance only for the coarse monotonic clock some platforms have.
    for position, start in enumerate(sorted(starts)):
        assert start - begun >= position * interval - 0.02


# ---------------------------------------------------------------------------
# RateLimiter: server-imposed cooldowns
# ---------------------------------------------------------------------------


def test_defer_holds_back_every_caller():
    fake = FakeClock()
    limiter = fake.limiter(1.0)

    limiter.acquire()
    limiter.defer(30.0)
    limiter.acquire()

    assert fake.sleeps == [30.0]


def test_defer_never_shortens_the_wait():
    fake = FakeClock()
    limiter = fake.limiter(10.0)

    limiter.acquire()
    limiter.defer(2.0)
    limiter.acquire()

    assert fake.sleeps == [10.0]


def test_defer_is_capped_so_one_absurd_retry_after_cannot_disable_a_source():
    fake = FakeClock()
    limiter = fake.limiter(0.0)

    limiter.defer(10**9)
    limiter.acquire()

    assert fake.sleeps == [rate_limit.MAX_COOLDOWN_SECONDS]


def test_acquire_refuses_a_wait_longer_than_allowed_without_taking_a_slot():
    fake = FakeClock()
    limiter = fake.limiter(1.0)
    limiter.defer(100.0)

    with pytest.raises(CooldownActive) as excinfo:
        limiter.acquire(max_wait_seconds=60.0)

    assert excinfo.value.wait_seconds == 100.0
    assert fake.sleeps == []
    fake.now = 100.0
    assert limiter.acquire(max_wait_seconds=60.0) == 0.0


# ---------------------------------------------------------------------------
# Retry-After and backoff
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7", 7.0),
        ("2.5", 2.5),
        ("0", 0.0),
        ("-5", 0.0),
        ("soon", None),
        ("nan", None),
        ("inf", None),
    ],
)
def test_retry_after_seconds_parses_delta_seconds(value, expected):
    assert rate_limit.retry_after_seconds({"Retry-After": value}) == expected


def test_retry_after_seconds_is_none_when_absent():
    assert rate_limit.retry_after_seconds({}) is None


def test_retry_after_seconds_parses_an_http_date():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    header = format_datetime(now + timedelta(seconds=20), usegmt=True)

    assert (
        rate_limit.retry_after_seconds({"Retry-After": header}, now=lambda: now) == 20.0
    )


def test_retry_delay_doubles_per_attempt():
    delays = [rate_limit.retry_delay({}, attempt, 2.0) for attempt in range(4)]

    assert delays == [2.0, 4.0, 8.0, 16.0]


def test_retry_delay_takes_the_longer_of_retry_after_and_backoff():
    assert rate_limit.retry_delay({"Retry-After": "30"}, 0, 2.0) == 30.0
    assert rate_limit.retry_delay({"Retry-After": "1"}, 2, 2.0) == 8.0


# ---------------------------------------------------------------------------
# send_with_retries
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, headers: dict | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False

    def close(self) -> None:
        self.closed = True


class CountingLimiter(RateLimiter):
    def __init__(self, fake: FakeClock, interval: float = 0.0) -> None:
        super().__init__(interval, clock=fake.clock, sleep=fake.sleep)
        self.acquired = 0

    def acquire(self, max_wait_seconds: float | None = None) -> float:
        self.acquired += 1
        return super().acquire(max_wait_seconds)


def _retry(limiter, responses, **kwargs):
    sent: list[int] = []

    def send():
        sent.append(len(sent))
        return responses[len(sent) - 1]

    options = {
        "source": "Test API",
        "max_retries": 3,
        "backoff_base_seconds": 1.0,
        "backoff_max_seconds": 60.0,
    } | kwargs
    return rate_limit.send_with_retries(send, limiter=limiter, **options), sent


def test_send_with_retries_returns_a_success_without_retrying():
    fake = FakeClock()
    limiter = CountingLimiter(fake)

    resp, sent = _retry(limiter, [FakeResponse(200)])

    assert resp.status_code == 200
    assert len(sent) == 1
    assert limiter.acquired == 1
    assert fake.sleeps == []


def test_send_with_retries_does_not_retry_a_403():
    fake = FakeClock()

    resp, sent = _retry(CountingLimiter(fake), [FakeResponse(403), FakeResponse(200)])

    assert resp.status_code == 403
    assert len(sent) == 1
    assert fake.sleeps == []


def test_every_retry_passes_through_the_limiter():
    fake = FakeClock()
    limiter = CountingLimiter(fake)
    throttled = FakeResponse(429)

    resp, sent = _retry(limiter, [throttled, FakeResponse(503), FakeResponse(200)])

    assert resp.status_code == 200
    assert limiter.acquired == len(sent) == 3
    assert fake.sleeps == [1.0, 2.0]
    assert throttled.closed  # a discarded answer does not hold its connection


def test_a_retry_still_honours_the_minimum_interval():
    """A backoff shorter than the spacing does not let a retry jump the queue."""
    fake = FakeClock()

    _retry(CountingLimiter(fake, interval=5.0), [FakeResponse(429), FakeResponse(200)])

    assert fake.sleeps == [5.0]


def test_retries_stop_at_the_cap_and_return_the_last_answer():
    fake = FakeClock()

    resp, sent = _retry(CountingLimiter(fake), [FakeResponse(429)] * 3, max_retries=2)

    assert resp.status_code == 429
    assert len(sent) == 3
    assert fake.sleeps == [1.0, 2.0]


def test_a_final_failure_still_holds_back_the_next_caller():
    fake = FakeClock()
    limiter = CountingLimiter(fake)

    _retry(limiter, [FakeResponse(429)], max_retries=0, backoff_base_seconds=4.0)
    limiter.acquire()

    assert fake.sleeps == [4.0]


def test_a_retry_after_beyond_the_cap_is_not_retried_early():
    fake = FakeClock()
    limiter = CountingLimiter(fake)

    resp, sent = _retry(
        limiter,
        [FakeResponse(429, {"Retry-After": "600"}), FakeResponse(200)],
        backoff_max_seconds=60.0,
    )

    assert resp.status_code == 429
    assert len(sent) == 1
    assert fake.sleeps == []
    # The server's cooldown is remembered: the next caller sends nothing.
    with pytest.raises(CooldownActive):
        _retry(limiter, [FakeResponse(200)], backoff_max_seconds=60.0)


# ---------------------------------------------------------------------------
# A 429 that carries no Retry-After (D-429-03)
#
# A server that refuses without saying when to come back has told us nothing a
# retry could act on, and retrying guesses at a cooldown we cannot observe. The
# opt-in below stops retrying such an answer and parks the source for the run
# instead. Default stays True, so Semantic Scholar's behaviour is unchanged.
# ---------------------------------------------------------------------------


def test_a_429_without_retry_after_is_not_retried_when_opted_out():
    fake = FakeClock()

    resp, sent = _retry(
        CountingLimiter(fake),
        [FakeResponse(429), FakeResponse(200)],
        retry_429_without_retry_after=False,
    )

    assert resp.status_code == 429
    assert len(sent) == 1
    assert fake.sleeps == []


def test_declining_to_retry_parks_the_source_for_the_whole_backoff_budget():
    """Not retrying must not mean coming back sooner: the limiter is held for
    the full backoff ceiling, which is longer than the exhausted-retry path
    would have deferred."""
    fake = FakeClock()
    limiter = CountingLimiter(fake)

    _retry(
        limiter,
        [FakeResponse(429)],
        backoff_max_seconds=60.0,
        retry_429_without_retry_after=False,
    )
    limiter.acquire()

    assert fake.sleeps == [60.0]


def test_a_429_carrying_a_retry_after_is_still_retried_when_opted_out():
    """The server named a time, so there is something to wait for."""
    fake = FakeClock()

    resp, sent = _retry(
        CountingLimiter(fake),
        [FakeResponse(429, {"Retry-After": "5"}), FakeResponse(200)],
        retry_429_without_retry_after=False,
    )

    assert resp.status_code == 200
    assert len(sent) == 2
    assert fake.sleeps == [5.0]


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_5xx_without_retry_after_is_still_retried_when_opted_out(status):
    """The opt-out is about 429 only; transient overload keeps its retries."""
    fake = FakeClock()

    resp, sent = _retry(
        CountingLimiter(fake),
        [FakeResponse(status), FakeResponse(200)],
        retry_429_without_retry_after=False,
    )

    assert resp.status_code == 200
    assert len(sent) == 2


def test_a_retryable_exception_is_still_retried_when_opted_out():
    """Transport failures are unrelated to the 429 opt-out (D-TIMEOUT-01)."""
    fake = FakeClock()

    resp, sent = _retry_raising(
        CountingLimiter(fake),
        [Boom(), FakeResponse(200)],
        retryable_exceptions=(Boom,),
        retry_429_without_retry_after=False,
    )

    assert resp.status_code == 200
    assert len(sent) == 2


def test_by_default_a_429_without_retry_after_is_still_retried():
    """Every caller that does not opt out — Semantic Scholar — is unchanged."""
    fake = FakeClock()

    resp, sent = _retry(CountingLimiter(fake), [FakeResponse(429), FakeResponse(200)])

    assert resp.status_code == 200
    assert len(sent) == 2


# ---------------------------------------------------------------------------
# send_with_retries: transport-level exceptions (D-TIMEOUT-01)
#
# A caller that does not pass `retryable_exceptions` (e.g. Semantic Scholar)
# must see byte-identical behaviour to before this option existed: any
# exception `send()` raises propagates immediately, uncaught.
# ---------------------------------------------------------------------------


class Boom(Exception):
    """A stand-in transient transport failure, e.g. requests.ReadTimeout."""


class Unrelated(Exception):
    """Not in `retryable_exceptions` — must never be caught."""


def _retry_raising(limiter, effects, **kwargs):
    """Like `_retry`, but `effects` entries may be exception instances to raise."""
    sent: list[int] = []

    def send():
        sent.append(len(sent))
        effect = effects[len(sent) - 1]
        if isinstance(effect, BaseException):
            raise effect
        return effect

    options = {
        "source": "Test API",
        "max_retries": 3,
        "backoff_base_seconds": 1.0,
        "backoff_max_seconds": 60.0,
    } | kwargs
    return rate_limit.send_with_retries(send, limiter=limiter, **options), sent


def test_a_transient_exception_not_opted_into_retryable_exceptions_propagates():
    """Default behaviour (no `retryable_exceptions` passed) is unchanged: a
    raised exception is not caught at all, matching every caller before this
    option existed."""
    fake = FakeClock()

    with pytest.raises(Boom):
        _retry_raising(CountingLimiter(fake), [Boom(), FakeResponse(200)])

    assert fake.sleeps == []


def test_a_retryable_exception_is_retried_and_then_succeeds():
    fake = FakeClock()
    limiter = CountingLimiter(fake)

    resp, sent = _retry_raising(
        limiter,
        [Boom(), FakeResponse(200)],
        retryable_exceptions=(Boom,),
    )

    assert resp.status_code == 200
    assert len(sent) == 2
    assert limiter.acquired == 2
    assert fake.sleeps == [1.0]  # backoff_base_seconds * 2**0


def test_retryable_exception_backoff_doubles_between_attempts():
    fake = FakeClock()

    _retry_raising(
        CountingLimiter(fake),
        [Boom(), Boom(), FakeResponse(200)],
        retryable_exceptions=(Boom,),
    )

    assert fake.sleeps == [1.0, 2.0]


def test_a_retryable_exception_is_still_raised_once_retries_are_exhausted():
    """There is no response to fall back to, so the exception itself is
    re-raised — not swallowed into a fabricated answer."""
    fake = FakeClock()
    limiter = CountingLimiter(fake)

    with pytest.raises(Boom):
        _retry_raising(
            limiter,
            [Boom(), Boom(), Boom()],
            retryable_exceptions=(Boom,),
            max_retries=1,
        )

    assert limiter.acquired == 2
    assert fake.sleeps == [1.0]


def test_a_retryable_exception_still_holds_back_the_next_caller():
    fake = FakeClock()
    limiter = CountingLimiter(fake)

    with pytest.raises(Boom):
        _retry_raising(limiter, [Boom()], retryable_exceptions=(Boom,), max_retries=0)
    limiter.acquire()

    assert fake.sleeps == [1.0]


def test_an_exception_type_outside_retryable_exceptions_is_not_caught():
    fake = FakeClock()

    with pytest.raises(Unrelated):
        _retry_raising(
            CountingLimiter(fake),
            [Unrelated(), FakeResponse(200)],
            retryable_exceptions=(Boom,),
        )

    assert fake.sleeps == []


def test_a_retryable_exception_honours_the_minimum_interval():
    """A backoff shorter than the spacing does not let a retry jump the queue,
    matching the equivalent 429 behaviour."""
    fake = FakeClock()

    _retry_raising(
        CountingLimiter(fake, interval=5.0),
        [Boom(), FakeResponse(200)],
        retryable_exceptions=(Boom,),
    )

    assert fake.sleeps == [5.0]


# ---------------------------------------------------------------------------
# send_with_retries: a caller's wait budget (Mode B's query deadline, D-B-04)
# ---------------------------------------------------------------------------


def test_a_wait_longer_than_the_budget_sends_nothing_and_reserves_nothing():
    fake = FakeClock()
    limiter = CountingLimiter(fake, interval=60.0)
    limiter.acquire()  # the previous call took the slot at t=0
    fake.now = 10.0  # next slot at t=60: a 50 s wait
    with pytest.raises(rate_limit.WaitBudgetExceeded) as raised:
        _retry(limiter, [FakeResponse(200)], wait_budget=lambda: 40.0)
    assert raised.value.wait_seconds == pytest.approx(50.0)
    assert fake.sleeps == []
    # Nothing was reserved: the next caller still gets the t=60 slot.
    assert limiter.acquire() == pytest.approx(50.0)


def test_a_wait_within_the_budget_proceeds_normally():
    fake = FakeClock()
    limiter = CountingLimiter(fake, interval=60.0)
    limiter.acquire()
    fake.now = 10.0
    response, sent = _retry(limiter, [FakeResponse(200)], wait_budget=lambda: 55.0)
    assert response.status_code == 200 and sent == [0]
    assert fake.sleeps == [pytest.approx(50.0)]


def test_a_spent_budget_sends_nothing_even_without_pacing():
    limiter = CountingLimiter(FakeClock())
    with pytest.raises(rate_limit.WaitBudgetExceeded):
        _retry(limiter, [FakeResponse(200)], wait_budget=lambda: 0.0)


def test_a_retry_whose_backoff_outlasts_the_budget_is_not_sent():
    fake = FakeClock()
    limiter = CountingLimiter(fake)
    remaining = {"t": 5.0}
    responses = [FakeResponse(429, {"Retry-After": "30"}), FakeResponse(200)]
    with pytest.raises(rate_limit.WaitBudgetExceeded):
        _retry(limiter, responses, wait_budget=lambda: remaining["t"])
    assert limiter.acquired == 2  # the retry asked for a slot and was refused


def test_a_server_cooldown_beyond_the_ceiling_is_still_a_cooldown_not_a_budget():
    fake = FakeClock()
    limiter = CountingLimiter(fake)
    limiter.defer(500.0)
    with pytest.raises(CooldownActive):
        _retry(limiter, [FakeResponse(200)], wait_budget=lambda: 100.0)
