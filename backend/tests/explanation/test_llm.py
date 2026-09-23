import json

import httpx
import pytest

from app.explanation.llm import (
    GroqClient,
    LLMAuthError,
    LLMError,
    LLMUnavailableError,
    get_llm_client,
)
from tests.ingestion.fake_clock import FakeClock


class FakeResponse:
    """The parts of an `httpx.Response` the client reads: status, headers, body."""

    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self.payload


class FakeHTTPClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.payload)


def _completion(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _client(http, **overrides):
    """A client on its own limiter, so no test inherits another's cooldown.

    `GroqClient` shares one process-wide limiter by default — Stage 9 and Mode
    B spend the same quota — which is exactly what a test must not join.
    """
    return GroqClient(
        "secret",
        "groq-test",
        http_client=http,
        limiter=FakeClock().limiter(),
        **overrides,
    )


def test_groq_posts_bearer_token_to_openai_compatible_endpoint():
    http = FakeHTTPClient(_completion(json.dumps({"answer": "grounded"})))
    client = _client(http)

    result = client.generate_json("prompt", {"type": "object"})

    assert result == {"answer": "grounded"}
    url, kwargs = http.calls[0]
    assert url == "https://api.groq.com/openai/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer secret"


def test_groq_hands_the_report_schema_to_structured_output():
    """The schema is enforced by the provider, not just described in the prompt."""
    http = FakeHTTPClient(_completion(json.dumps({"answer": "grounded"})))
    client = _client(http)

    schema = {"type": "object", "properties": {"gap": {"type": "string"}}}
    client.generate_json("prompt", schema)

    body = http.calls[0][1]["json"]
    assert body["model"] == "groq-test"
    assert body["messages"] == [{"role": "user", "content": "prompt"}]
    assert body["stream"] is False
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == schema


def test_groq_requires_an_api_key():
    with pytest.raises(LLMError, match="GROQ_API_KEY"):
        GroqClient("", "groq-test")


def test_groq_rejects_a_response_that_is_not_json():
    http = FakeHTTPClient(_completion("not json at all"))
    client = _client(http)

    with pytest.raises(LLMError, match="invalid structured JSON"):
        client.generate_json("prompt", {"type": "object"})


class RefusingHTTPClient:
    def post(self, url, **kwargs):
        request = httpx.Request("POST", url)
        raise httpx.ConnectError("Connection refused", request=request)


def test_groq_connection_failure_becomes_actionable_provider_error():
    client = _client(RefusingHTTPClient(), max_retries=0)

    with pytest.raises(LLMUnavailableError, match="Groq is unavailable"):
        client.generate_json("prompt", {"type": "object"})


class StatusHTTPClient:
    def __init__(self, status_code):
        self.status_code = status_code

    def post(self, url, **kwargs):
        return FakeResponse({}, status_code=self.status_code)


def test_groq_rejects_a_bad_key_as_an_authentication_failure():
    """Not an outage: retrying cannot help, and the operator must change a key.

    The rate-limit and classification cases live in `test_llm_rate_limit.py`.
    """
    client = _client(StatusHTTPClient(401))

    with pytest.raises(LLMAuthError, match="GROQ_API_KEY"):
        client.generate_json("prompt", {"type": "object"})


class FakeSettings:
    """Only the fields `get_llm_client` reads — `Settings` itself requires a
    full environment, which this test does not need."""

    def __init__(self, **overrides):
        self.llm_provider = "groq"
        self.llm_api_key = None
        self.llm_timeout_seconds = 120.0
        self.groq_api_key = None
        self.groq_model = "groq-default"
        self.groq_api_url = "https://api.groq.com/openai/v1"
        self.groq_max_retries = 4
        self.groq_backoff_base_seconds = 2.0
        self.groq_backoff_max_seconds = 90.0
        self.groq_min_interval_seconds = 0.0
        self.groq_max_completion_tokens = 2048
        self.__dict__.update(overrides)


def test_get_llm_client_builds_a_groq_client_from_settings():
    settings = FakeSettings(groq_api_key="key-from-env", groq_model="groq-configured")

    client = get_llm_client(settings)

    assert isinstance(client, GroqClient)
    assert client.provider == "groq"
    assert client.model == "groq-configured"
    assert client.api_key == "key-from-env"


def test_get_llm_client_falls_back_to_the_generic_provider_credential():
    settings = FakeSettings(llm_api_key="generic-key")

    assert get_llm_client(settings).api_key == "generic-key"


def test_get_llm_client_rejects_any_provider_other_than_groq():
    settings = FakeSettings(llm_provider="gemini", groq_api_key="key")

    with pytest.raises(LLMError, match="groq"):
        get_llm_client(settings)
