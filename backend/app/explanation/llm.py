"""Direct Groq structured-output client.

Groq is the project's only LLM provider. It is called over its
OpenAI-compatible REST API with `httpx` — no vendor SDK and no framework
wrapper (CLAUDE.md hard constraint 1, README §7).
"""

import json
from typing import Protocol

import httpx

from app.core.config import Settings, get_settings


class LLMError(RuntimeError):
    pass


class LLMUnavailableError(LLMError):
    """The configured provider cannot currently serve generation requests."""


class StructuredLLM(Protocol):
    provider: str
    model: str

    def generate_json(self, prompt: str, schema: dict) -> dict: ...


class GroqClient:
    provider = "groq"

    def __init__(
        self,
        api_key: str,
        model: str,
        api_url: str = "https://api.groq.com/openai/v1",
        timeout: float = 120.0,
        http_client=None,
    ) -> None:
        if not api_key:
            raise LLMError("GROQ_API_KEY is required to call the Groq API")
        self.api_key = api_key
        self.model = model
        self.api_url = api_url.rstrip("/")
        self.http = http_client or httpx.Client(timeout=timeout)

    def generate_json(self, prompt: str, schema: dict) -> dict:
        try:
            response = self.http.post(
                f"{self.api_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "temperature": 0,
                    # Groq's structured-output mode: the schema is enforced by
                    # the provider rather than only described in the prompt.
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "report",
                            "strict": False,
                            "schema": schema,
                        },
                    },
                },
            )
            response.raise_for_status()
        except httpx.RequestError as exc:
            raise LLMUnavailableError(
                "Groq is unavailable. Check network access and the configured provider."
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise LLMUnavailableError(
                    "Groq rejected the configured GROQ_API_KEY."
                ) from exc
            if status == 429:
                raise LLMUnavailableError(
                    "Groq rate limit reached. Retry later or lower REPORT_TOP_K."
                ) from exc
            raise
        try:
            return json.loads(response.json()["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError("Groq returned invalid structured JSON") from exc


def get_llm_client(settings: Settings | None = None) -> StructuredLLM:
    settings = settings or get_settings()
    provider = settings.llm_provider.casefold().strip()
    if provider != "groq":
        raise LLMError("LLM_PROVIDER must be 'groq' — the only supported provider")
    return GroqClient(
        settings.groq_api_key or settings.llm_api_key or "",
        settings.groq_model,
        settings.groq_api_url,
        settings.llm_timeout_seconds,
    )
