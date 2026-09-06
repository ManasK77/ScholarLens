import json

import httpx
import pytest

from app.explanation.llm import GeminiClient, LLMUnavailableError, OllamaClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeHTTPClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.payload)


def test_gemini_uses_api_key_header_and_schema_output():
    http = FakeHTTPClient({"output_text": json.dumps({"answer": "grounded"})})
    client = GeminiClient("secret", "gemini-test", http_client=http)

    result = client.generate_json("prompt", {"type": "object"})

    assert result == {"answer": "grounded"}
    assert http.calls[0][1]["headers"]["x-goog-api-key"] == "secret"


def test_gemini_reads_text_from_rest_interaction_steps():
    http = FakeHTTPClient(
        {
            "steps": [
                {
                    "type": "model_output",
                    "content": [
                        {"type": "text", "text": json.dumps({"answer": "grounded"})}
                    ],
                }
            ]
        }
    )
    client = GeminiClient("secret", "gemini-test", http_client=http)

    assert client.generate_json("prompt", {"type": "object"}) == {"answer": "grounded"}


def test_ollama_uses_local_chat_endpoint_and_json_schema():
    http = FakeHTTPClient({"message": {"content": json.dumps({"answer": "local"})}})
    client = OllamaClient("http://ollama:11434", "qwen3", http_client=http)

    result = client.generate_json("prompt", {"type": "object"})

    assert result == {"answer": "local"}
    assert http.calls[0][0] == "http://ollama:11434/api/chat"
    assert http.calls[0][1]["json"]["stream"] is False


class RefusingHTTPClient:
    def post(self, url, **kwargs):
        request = httpx.Request("POST", url)
        raise httpx.ConnectError("Connection refused", request=request)


def test_ollama_connection_failure_becomes_actionable_provider_error():
    client = OllamaClient(
        "http://ollama:11434", "qwen3:8b", http_client=RefusingHTTPClient()
    )

    with pytest.raises(LLMUnavailableError, match="Ollama is unavailable"):
        client.generate_json("prompt", {"type": "object"})
