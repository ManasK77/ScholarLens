"""Direct Gemini and Ollama structured-output clients."""

import json
from typing import Protocol

import httpx

from app.core.config import Settings, get_settings


class LLMError(RuntimeError):
    pass


class StructuredLLM(Protocol):
    provider: str
    model: str

    def generate_json(self, prompt: str, schema: dict) -> dict: ...


class GeminiClient:
    provider = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str,
        api_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout: float = 120.0,
        http_client=None,
    ) -> None:
        if not api_key:
            raise LLMError("GEMINI_API_KEY is required when LLM_PROVIDER=gemini")
        self.api_key = api_key
        self.model = model
        self.api_url = api_url.rstrip("/")
        self.http = http_client or httpx.Client(timeout=timeout)

    def generate_json(self, prompt: str, schema: dict) -> dict:
        response = self.http.post(
            f"{self.api_url}/interactions",
            headers={"x-goog-api-key": self.api_key},
            json={
                "model": self.model,
                "input": prompt,
                "response_format": {
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": schema,
                },
            },
        )
        response.raise_for_status()
        body = response.json()
        text = body.get("output_text")
        if text is None:
            model_steps = [
                step
                for step in body.get("steps", [])
                if step.get("type") == "model_output"
            ]
            if model_steps:
                text = "".join(
                    block.get("text", "")
                    for block in model_steps[-1].get("content", [])
                    if block.get("type") == "text"
                )
        if text is None:
            raise LLMError("Gemini response contained no text output")
        try:
            return json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise LLMError("Gemini returned invalid structured JSON") from exc


class OllamaClient:
    provider = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float = 120.0,
        http_client=None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.http = http_client or httpx.Client(timeout=timeout)

    def generate_json(self, prompt: str, schema: dict) -> dict:
        response = self.http.post(
            f"{self.base_url}/api/chat",
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "format": schema,
                "options": {"temperature": 0},
            },
        )
        response.raise_for_status()
        try:
            return json.loads(response.json()["message"]["content"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError("Ollama returned invalid structured JSON") from exc


def get_llm_client(settings: Settings | None = None) -> StructuredLLM:
    settings = settings or get_settings()
    provider = settings.llm_provider.casefold().strip()
    if provider == "gemini":
        return GeminiClient(
            settings.gemini_api_key or settings.llm_api_key or "",
            settings.gemini_model,
            settings.gemini_api_url,
            settings.llm_timeout_seconds,
        )
    if provider == "ollama":
        return OllamaClient(
            settings.ollama_url,
            settings.ollama_model,
            settings.llm_timeout_seconds,
        )
    raise LLMError("LLM_PROVIDER must be either 'gemini' or 'ollama'")
