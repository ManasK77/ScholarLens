"""How `GroqClient` behaves when Groq meters us — the Stage 9 failure mode.

Stage 9 sends `report_top_k` generations back to back, each carrying up to
`report_evidence_top_n` retrieved passages, and Groq's free tier meters
*tokens* per minute as well as requests per day. So a 429 partway through a run
is an ordinary, recoverable condition rather than an outage: it is retried on
the provider's own schedule, through the one limiter every Groq caller shares,
within a bounded budget.

The classification tests alongside them exist because "LLM provider
unavailable" was previously the answer to a rejected key, an unknown model and
a rate limit alike, which tells an operator nothing about what to do next.

Everything runs on a fake clock; no test sleeps for real.
"""

import json
import logging

import httpx
import pytest

from app.explanation.llm import (
    GroqClient,
    LLMAuthError,
    LLMConfigurationError,
    LLMError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from tests.ingestion.fake_clock import FakeClock


class ScriptedHTTPClient:
    """Returns the scripted `httpx.Response`s in order, recording every call.

    Real `httpx.Response` objects rather than doubles: the retry path reads
    `.status_code` and `.headers` and the classifier reads the JSON body, so a
    stand-in that got those shapes wrong would be testing itself.
    """

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        response.request = httpx.Request("POST", url)
        return response


class RefusingHTTPClient:
    def post(self, url, **kwargs):
        raise httpx.ConnectError(
            "Connection refused", request=httpx.Request("POST", url)
        )


TPM_MESSAGE = (
    "Rate limit reached for model `openai/gpt-oss-20b` in organization `org_x` "
    "on tokens per minute (TPM): Limit 8000, Used 7500, Requested 2900."
)


def _ok(payload: dict | None = None) -> httpx.Response:
    content = json.dumps(payload if payload is not None else {"gap": "grounded"})
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _429(retry_after: str | None = None, message: str = TPM_MESSAGE) -> httpx.Response:
    return httpx.Response(
        429,
        json={"error": {"message": message, "code": "rate_limit_exceeded"}},
        headers={} if retry_after is None else {"Retry-After": retry_after},
    )


def _client(http, fake, **overrides):
    """A `GroqClient` wired to a fake clock and its own limiter."""
    options = {
        "http_client": http,
        "limiter": fake.limiter(overrides.pop("min_interval_seconds", 0.0)),
        "max_retries": 3,
        "backoff_base_seconds": 2.0,
        "backoff_max_seconds": 90.0,
    }
    options.update(overrides)
    return GroqClient("secret", "groq-test", **options)


# ---------------------------------------------------------------------------
# Retry and backoff
# ---------------------------------------------------------------------------


def test_a_rate_limited_request_is_retried_rather_than_failing_the_run():
    fake = FakeClock()
    http = ScriptedHTTPClient(_429(retry_after="5"), _ok())

    result = _client(http, fake).generate_json("prompt", {"type": "object"})

    assert result == {"gap": "grounded"}
    assert len(http.calls) == 2


def test_retry_after_is_honoured_when_it_exceeds_our_own_backoff():
    """The provider knows when its window resets; an exponential curve guesses."""
    fake = FakeClock()
    http = ScriptedHTTPClient(_429(retry_after="47"), _ok())

    _client(http, fake).generate_json("prompt", {"type": "object"})

    assert fake.sleeps == [47.0]


def test_our_backoff_is_used_when_the_provider_names_no_retry_after():
    fake = FakeClock()
    http = ScriptedHTTPClient(_429(retry_after=None), _ok())

    _client(http, fake, backoff_base_seconds=4.0).generate_json(
        "prompt", {"type": "object"}
    )

    assert fake.sleeps == [4.0]


def test_backoff_grows_exponentially_across_consecutive_rate_limits():
    """No retry storm: each refusal costs the next one twice the wait."""
    fake = FakeClock()
    http = ScriptedHTTPClient(_429(), _429(), _429(), _ok())

    _client(http, fake, backoff_base_seconds=2.0).generate_json(
        "prompt", {"type": "object"}
    )

    assert fake.sleeps == [2.0, 4.0, 8.0]


def test_retrying_is_bounded_and_ends_in_a_rate_limit_error():
    """A budget, not an indefinite loop: the first try plus `max_retries`."""
    fake = FakeClock()
    http = ScriptedHTTPClient(_429(retry_after="3"))

    with pytest.raises(LLMRateLimitError) as caught:
        _client(http, fake, max_retries=3).generate_json("prompt", {"type": "object"})

    assert len(http.calls) == 4
    assert caught.value.retry_after == 3.0


def test_a_retry_after_beyond_the_budget_is_reported_not_waited_out():
    """A daily quota answers in hours. Say so; do not hold the request open."""
    fake = FakeClock()
    http = ScriptedHTTPClient(_429(retry_after="7200"))

    with pytest.raises(LLMRateLimitError) as caught:
        _client(http, fake, backoff_max_seconds=90.0).generate_json(
            "prompt", {"type": "object"}
        )

    assert len(http.calls) == 1
    assert fake.sleeps == []
    assert caught.value.retry_after == 7200.0


def test_the_rate_limit_error_carries_the_limit_the_provider_named():
    """Which ceiling was hit — TPM, RPM, TPD — is the whole diagnosis."""
    fake = FakeClock()
    http = ScriptedHTTPClient(_429())

    with pytest.raises(LLMRateLimitError, match="tokens per minute"):
        _client(http, fake, max_retries=0).generate_json("prompt", {"type": "object"})


def test_a_rate_limit_error_is_still_a_provider_unavailable_error():
    """Callers that only care about "cannot generate right now" keep working."""
    assert issubclass(LLMRateLimitError, LLMUnavailableError)


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [401, 403])
def test_a_rejected_key_is_an_auth_error_not_an_outage(status_code):
    fake = FakeClock()
    http = ScriptedHTTPClient(httpx.Response(status_code, json={}))

    with pytest.raises(LLMAuthError, match="GROQ_API_KEY"):
        _client(http, fake).generate_json("prompt", {"type": "object"})

    assert len(http.calls) == 1, "a rejected key will not fix itself; do not retry"
    assert not issubclass(LLMAuthError, LLMUnavailableError)


def test_an_unknown_model_is_a_configuration_error():
    fake = FakeClock()
    http = ScriptedHTTPClient(
        httpx.Response(
            404, json={"error": {"message": "The model `nope` does not exist"}}
        )
    )

    with pytest.raises(LLMConfigurationError, match="does not exist"):
        _client(http, fake).generate_json("prompt", {"type": "object"})

    assert len(http.calls) == 1


def test_a_rejected_request_body_is_a_configuration_error():
    fake = FakeClock()
    http = ScriptedHTTPClient(
        httpx.Response(400, json={"error": {"message": "unsupported response_format"}})
    )

    with pytest.raises(LLMConfigurationError):
        _client(http, fake).generate_json("prompt", {"type": "object"})


def test_a_server_error_is_retried_and_then_reported_as_unavailable():
    fake = FakeClock()
    http = ScriptedHTTPClient(httpx.Response(503, json={}))

    with pytest.raises(LLMUnavailableError, match="unavailable"):
        _client(http, fake, max_retries=2).generate_json("prompt", {"type": "object"})

    assert len(http.calls) == 3


def test_a_transient_server_error_recovers_without_failing_the_run():
    fake = FakeClock()
    http = ScriptedHTTPClient(httpx.Response(500, json={}), _ok())

    result = _client(http, fake).generate_json("prompt", {"type": "object"})

    assert result == {"gap": "grounded"}


def test_a_network_failure_is_unavailable_and_not_confused_with_a_rate_limit():
    fake = FakeClock()
    client = _client(RefusingHTTPClient(), fake, max_retries=0)

    with pytest.raises(LLMUnavailableError, match="Groq is unavailable") as caught:
        client.generate_json("prompt", {"type": "object"})

    assert not isinstance(caught.value, LLMRateLimitError)


def test_an_unexpected_status_is_neither_swallowed_nor_misreported():
    fake = FakeClock()
    http = ScriptedHTTPClient(httpx.Response(418, json={}))

    with pytest.raises(LLMError) as caught:
        _client(http, fake).generate_json("prompt", {"type": "object"})

    assert not isinstance(caught.value, LLMUnavailableError)
    assert "418" in str(caught.value)


# ---------------------------------------------------------------------------
# Token budget and pacing — the two levers on tokens-per-minute pressure
# ---------------------------------------------------------------------------


def test_the_completion_is_capped_so_one_report_cannot_drain_the_minute():
    """`openai/gpt-oss-20b` reasons before it answers, and those hidden tokens
    count against the same TPM ceiling as the answer. Uncapped, one report's
    spend is unbounded."""
    fake = FakeClock()
    http = ScriptedHTTPClient(_ok())

    _client(http, fake, max_completion_tokens=1234).generate_json(
        "prompt", {"type": "object"}
    )

    assert http.calls[0][1]["json"]["max_completion_tokens"] == 1234


def test_no_completion_cap_is_sent_when_none_is_configured():
    fake = FakeClock()
    http = ScriptedHTTPClient(_ok())

    _client(http, fake, max_completion_tokens=None).generate_json(
        "prompt", {"type": "object"}
    )

    assert "max_completion_tokens" not in http.calls[0][1]["json"]


def test_a_completion_cut_off_by_the_cap_says_so_instead_of_invalid_json():
    fake = FakeClock()
    http = ScriptedHTTPClient(
        httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"gap": "half'}, "finish_reason": "length"}
                ]
            },
        )
    )

    with pytest.raises(LLMError, match="truncated"):
        _client(http, fake).generate_json("prompt", {"type": "object"})


def test_requests_can_be_paced_so_a_run_spreads_across_the_limit_window():
    """Opt-in proactive spacing, for a tier whose TPM a burst of K reports exceeds."""
    fake = FakeClock()
    http = ScriptedHTTPClient(_ok())
    client = _client(http, fake, min_interval_seconds=20.0)

    for _ in range(3):
        client.generate_json("prompt", {"type": "object"})

    assert fake.sleeps == [20.0, 20.0]


def test_every_client_shares_one_limiter_so_mode_a_and_mode_b_do_not_collide():
    """Stage 9 and `/query` draw on the same quota; pacing has to be process-wide."""
    first = GroqClient("secret", "groq-test", http_client=ScriptedHTTPClient(_ok()))
    second = GroqClient("secret", "groq-test", http_client=ScriptedHTTPClient(_ok()))

    assert first.limiter is second.limiter


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def test_no_failure_path_writes_the_api_key_or_a_bearer_header_to_the_log(caplog):
    fake = FakeClock()
    http = ScriptedHTTPClient(_429(retry_after="1"))

    with caplog.at_level(logging.DEBUG), pytest.raises(LLMRateLimitError):
        _client(http, fake, max_retries=1).generate_json("p", {"type": "object"})

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "secret" not in logged
    assert "Bearer" not in logged
