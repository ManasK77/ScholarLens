"""How `GroqClient` classifies a generation it could not use (D-09-07).

The first clean baseline asked for five reports and stored two. Two clusters
came back as HTTP 400 "Failed to validate JSON ... See 'failed_generation'"
and one as `finish_reason: "length"`. The 400s were reported as
`LLMConfigurationError` -- "check GROQ_MODEL and that it supports
response_format: json_schema" -- although the same model, schema and request
shape produced the other two reports in the same run. Groq's best-effort
structured output (`strict: false`) validates the finished generation against
the schema and answers 400 `json_validate_failed` when it does not match, so
that is an *output* failure of one generation, not a rejected request.

These tests pin the classification, the diagnostic the next real run needs to
confirm the cause, and that nothing here adds a Groq call.
"""

import json
import logging

import httpx
import pytest

from app.explanation.llm import (
    GroqClient,
    LLMConfigurationError,
    LLMError,
    LLMOutputError,
    LLMTruncatedError,
    LLMUnavailableError,
)
from app.explanation.schema import REPORT_SCHEMA
from tests.ingestion.fake_clock import FakeClock

VALIDATE_MESSAGE = (
    "Failed to validate JSON. Please adjust your prompt. "
    "See 'failed_generation' for more details."
)


class ScriptedHTTPClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        response.request = httpx.Request("POST", url)
        return response


def _client(http, **overrides):
    options = {
        "http_client": http,
        "limiter": FakeClock().limiter(),
        "max_retries": 3,
    }
    options.update(overrides)
    return GroqClient("secret", "openai/gpt-oss-20b", **options)


def _json_validate_failed(
    failed_generation: str | None, *, code=True
) -> httpx.Response:
    error = {"message": VALIDATE_MESSAGE, "type": "invalid_request_error"}
    if code:
        error["code"] = "json_validate_failed"
    if failed_generation is not None:
        error["failed_generation"] = failed_generation
    return httpx.Response(400, json={"error": error})


# ---------------------------------------------------------------------------
# A. JSON-schema validation failure
# ---------------------------------------------------------------------------


def test_json_validate_failed_is_an_output_error_not_a_configuration_error():
    http = ScriptedHTTPClient(_json_validate_failed('{"gap": "half'))

    with pytest.raises(LLMOutputError) as caught:
        _client(http).generate_json("prompt", REPORT_SCHEMA)

    assert not isinstance(caught.value, LLMConfigurationError)
    assert not isinstance(caught.value, LLMUnavailableError)
    assert "GROQ_MODEL" not in str(caught.value)
    assert "did not validate" in str(caught.value)


def test_json_validate_failed_is_recognised_by_its_message_alone():
    http = ScriptedHTTPClient(_json_validate_failed(None, code=False))

    with pytest.raises(LLMOutputError):
        _client(http).generate_json("prompt", REPORT_SCHEMA)


def test_json_validate_failed_is_not_retried():
    """Temperature 0 on the same prompt would fail the same way: a retry only
    spends another paced call."""
    http = ScriptedHTTPClient(_json_validate_failed('{"gap": "half'))

    with pytest.raises(LLMOutputError):
        _client(http).generate_json("prompt", REPORT_SCHEMA)

    assert len(http.calls) == 1


def test_a_cut_off_failed_generation_is_described_as_unparseable():
    """The diagnostic that tells truncation apart from a schema mismatch."""
    partial = '{"gap": "Multilingual defenses remain'
    http = ScriptedHTTPClient(_json_validate_failed(partial))

    with pytest.raises(LLMOutputError) as caught:
        _client(http).generate_json("prompt", REPORT_SCHEMA)

    message = str(caught.value)
    assert "not parseable JSON" in message
    assert f"{len(partial)} chars" in message


def test_a_complete_but_non_conforming_generation_names_the_missing_keys():
    generation = json.dumps(
        {"gap": "x", "confidence": 0.4, "suggested_directions": ["y"]}
    )
    http = ScriptedHTTPClient(_json_validate_failed(generation))

    with pytest.raises(LLMOutputError) as caught:
        _client(http).generate_json("prompt", REPORT_SCHEMA)

    message = str(caught.value)
    assert "missing required key(s)" in message
    assert "evidence_ids" in message
    assert "problem_evidence" in message


def test_the_failed_generation_itself_never_reaches_the_exception():
    """The API returns this message; corpus text must not ride along in it."""
    generation = '{"gap": "SECRET-LOOKING corpus sentence that is long"'
    http = ScriptedHTTPClient(_json_validate_failed(generation))

    with pytest.raises(LLMOutputError) as caught:
        _client(http).generate_json("prompt", REPORT_SCHEMA)

    assert "SECRET-LOOKING" not in str(caught.value)


def test_the_failed_generation_is_logged_bounded_for_diagnosis(caplog):
    generation = '{"gap": "' + "x" * 5000
    http = ScriptedHTTPClient(_json_validate_failed(generation))

    with (
        caplog.at_level(logging.WARNING, logger="app.explanation.llm"),
        pytest.raises(LLMOutputError),
    ):
        _client(http).generate_json("prompt", REPORT_SCHEMA)

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "failed_generation" in logged
    assert len(logged) < 1500


def test_any_other_400_is_still_a_configuration_error():
    http = ScriptedHTTPClient(
        httpx.Response(400, json={"error": {"message": "unsupported response_format"}})
    )

    with pytest.raises(LLMConfigurationError):
        _client(http).generate_json("prompt", REPORT_SCHEMA)


# ---------------------------------------------------------------------------
# B. Truncation by max_completion_tokens
# ---------------------------------------------------------------------------


def _truncated(usage: dict | None = None, content: str = "") -> httpx.Response:
    body = {"choices": [{"message": {"content": content}, "finish_reason": "length"}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def test_truncation_is_an_output_error_that_names_the_setting():
    http = ScriptedHTTPClient(_truncated())

    with pytest.raises(LLMTruncatedError) as caught:
        _client(http, max_completion_tokens=2048).generate_json("prompt", REPORT_SCHEMA)

    assert isinstance(caught.value, LLMOutputError)
    assert "GROQ_MAX_COMPLETION_TOKENS" in str(caught.value)
    assert "2048" in str(caught.value)


def test_truncation_reports_how_much_of_the_budget_reasoning_spent():
    """The baseline's open question: did hidden reasoning eat the budget?"""
    http = ScriptedHTTPClient(
        _truncated(
            {
                "prompt_tokens": 2600,
                "completion_tokens": 2048,
                "completion_tokens_details": {"reasoning_tokens": 1990},
            }
        )
    )

    with pytest.raises(LLMTruncatedError) as caught:
        _client(http, max_completion_tokens=2048).generate_json("prompt", REPORT_SCHEMA)

    assert "reasoning used 1990 of 2048" in str(caught.value)


def test_truncation_is_not_retried():
    http = ScriptedHTTPClient(_truncated())

    with pytest.raises(LLMTruncatedError):
        _client(http).generate_json("prompt", REPORT_SCHEMA)

    assert len(http.calls) == 1


def test_token_usage_is_logged_for_a_successful_generation(caplog):
    body = {
        "choices": [{"message": {"content": '{"gap": "g"}'}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 2400,
            "completion_tokens": 1500,
            "completion_tokens_details": {"reasoning_tokens": 900},
        },
    }
    http = ScriptedHTTPClient(httpx.Response(200, json=body))

    with caplog.at_level(logging.INFO, logger="app.explanation.llm"):
        _client(http).generate_json("prompt", REPORT_SCHEMA)

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "prompt=2400" in logged
    assert "completion=1500" in logged
    assert "reasoning=900" in logged


# ---------------------------------------------------------------------------
# Request too large for the per-minute token budget
# ---------------------------------------------------------------------------


def test_a_413_names_the_token_budget_and_is_not_an_outage():
    """Groq sizes a request as prompt + max_completion_tokens against TPM."""
    http = ScriptedHTTPClient(
        httpx.Response(
            413,
            json={
                "error": {
                    "message": "Request too large for model `openai/gpt-oss-20b` on "
                    "tokens per minute (TPM): Limit 8000, Requested 8300"
                }
            },
        )
    )

    with pytest.raises(LLMError) as caught:
        _client(http).generate_json("prompt", REPORT_SCHEMA)

    assert not isinstance(caught.value, LLMUnavailableError)
    assert not isinstance(caught.value, LLMConfigurationError)
    assert "GROQ_MAX_COMPLETION_TOKENS" in str(caught.value)
    assert len(http.calls) == 1
