"""Direct Groq structured-output client.

Groq is the project's only LLM provider. It is called over its
OpenAI-compatible REST API with `httpx` — no vendor SDK and no framework
wrapper (CLAUDE.md hard constraint 1, README §7).

**Rate limiting.** Stage 9 sends `report_top_k` generations back to back and
Mode B adds more, while Groq's free tier meters *tokens* per minute as well as
requests per day. A 429 partway through a run is therefore an ordinary,
recoverable condition, not an outage. Every request goes through
`app.ingestion.rate_limit` — the same bounded retry loop, Retry-After handling
and process-wide limiter the discovery sources use, rather than a second
implementation of it. The limiter is module-level so Stage 9 and `/query`,
which draw on one quota, queue behind each other instead of each behaving as
though it were the only caller.

**Classification.** A rejected key, an unknown model and an exhausted quota all
used to surface as "LLM provider unavailable", which tells an operator nothing
about what to do next. The exception hierarchy below separates what will fix
itself given time from what needs a configuration change, and a rate limit
carries the provider's own message (which ceiling, what was used) and the
Retry-After it named.
"""

import json
import logging
from typing import Protocol

import httpx

from app.core.config import Settings, get_settings
from app.ingestion.rate_limit import (
    CooldownActive,
    RateLimiter,
    retry_after_seconds,
    send_with_retries,
)

logger = logging.getLogger(__name__)

# How much of the provider's own error text is carried into our exception. It
# is Groq's diagnostic prose — which limit was hit, what was used, which model
# — and carries no credential, but it is bounded so a pathological body cannot
# become an unbounded API response or log line.
PROVIDER_MESSAGE_CHARS = 300


class LLMError(RuntimeError):
    """Any failure to obtain a usable generation. Mapped to HTTP 502."""


class LLMConfigurationError(LLMError):
    """The request is wrong and will stay wrong: unknown model, rejected body.

    Deliberately *not* an `LLMUnavailableError`. Retrying cannot help, and
    telling the operator to "retry later" would send them to wait out a
    misconfiguration.
    """


class LLMAuthError(LLMError):
    """Groq rejected the credential. Also not transient — the key must change."""


class LLMUnavailableError(LLMError):
    """The provider cannot currently serve generation requests.

    Transient by definition: a network failure, a 5xx, or a quota that will
    refill. Mapped to HTTP 503 (or 429 for its rate-limit subclass).
    """


class LLMRateLimitError(LLMUnavailableError):
    """The retry budget was spent and Groq is still metering us.

    `retry_after` is what the provider asked for, in seconds, when it said —
    `None` when it did not. Minutes mean a per-minute ceiling the next run will
    clear; hours mean a daily quota, and no amount of waiting inside one
    request will help.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message)


class StructuredLLM(Protocol):
    provider: str
    model: str

    def generate_json(self, prompt: str, schema: dict) -> dict: ...


# One limiter for the whole process: Stage 9 and Mode B spend the same quota,
# so a 429 against either has to slow down both. Its interval is set from
# settings each time a client is built (`groq_min_interval_seconds`), and is 0
# — no proactive spacing, retry only — unless a deployment asks for more.
_GROQ_LIMITER = RateLimiter(0.0)


def _provider_message(response: httpx.Response) -> str:
    """Groq's own `error.message`, or "" if the body is not the shape we expect.

    Never raises: this runs on the failure path, where a malformed body is
    exactly what might be arriving.
    """
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str):
        return ""
    return message.strip()[:PROVIDER_MESSAGE_CHARS]


def _detail(response: httpx.Response) -> str:
    """` (<provider message>)` when Groq explained itself, otherwise ""."""
    message = _provider_message(response)
    return f" ({message})" if message else ""


class GroqClient:
    provider = "groq"

    def __init__(
        self,
        api_key: str,
        model: str,
        api_url: str = "https://api.groq.com/openai/v1",
        timeout: float = 120.0,
        http_client=None,
        *,
        max_retries: int = 4,
        backoff_base_seconds: float = 2.0,
        backoff_max_seconds: float = 90.0,
        min_interval_seconds: float = 0.0,
        max_completion_tokens: int | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        if not api_key:
            raise LLMError("GROQ_API_KEY is required to call the Groq API")
        self.api_key = api_key
        self.model = model
        self.api_url = api_url.rstrip("/")
        self.http = http_client or httpx.Client(timeout=timeout)
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        # Above a minute on purpose: a tokens-per-minute ceiling clears in 60s,
        # so a shorter ceiling would abandon the one refusal most worth waiting
        # out. A daily quota asks for hours, exceeds this, and is reported
        # rather than slept on.
        self.backoff_max_seconds = backoff_max_seconds
        self.max_completion_tokens = max_completion_tokens
        # `limiter` is injectable for tests; production shares the module one,
        # whose interval follows whichever client was built most recently —
        # they are all built from the same settings.
        if limiter is not None:
            self.limiter = limiter
        else:
            self.limiter = _GROQ_LIMITER
            self.limiter.set_min_interval(min_interval_seconds)

    def _body(self, prompt: str, schema: dict) -> dict:
        body = {
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
        }
        if self.max_completion_tokens is not None:
            # The default model reasons before it answers, and those hidden
            # tokens are billed against the same per-minute ceiling as the
            # answer. Uncapped, one report's token spend is unbounded, which is
            # what turns `report_top_k` back-to-back calls into a 429.
            body["max_completion_tokens"] = self.max_completion_tokens
        return body

    def generate_json(self, prompt: str, schema: dict) -> dict:
        try:
            response = send_with_retries(
                lambda: self.http.post(
                    f"{self.api_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=self._body(prompt, schema),
                ),
                limiter=self.limiter,
                source="groq",
                max_retries=self.max_retries,
                backoff_base_seconds=self.backoff_base_seconds,
                backoff_max_seconds=self.backoff_max_seconds,
                # A read timeout or dropped connection is as transient as a
                # 5xx and is retried on the same curve.
                retryable_exceptions=(httpx.RequestError,),
            )
        except CooldownActive as exc:
            # An earlier caller was metered and the limiter is still parked.
            # Sending now would ignore a wait we already know Groq wants.
            raise LLMRateLimitError(
                "Groq is rate limited; the next request is not due for "
                f"{exc.wait_seconds:.0f}s.",
                retry_after=exc.wait_seconds,
            ) from exc
        except httpx.RequestError as exc:
            raise LLMUnavailableError(
                "Groq is unavailable. Check network access and the configured provider."
            ) from exc

        self._raise_for_status(response)
        return self._parse(response)

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Turn a non-2xx answer into the most specific error we can name."""
        status = response.status_code
        if status < 400:
            return
        if status == 429:
            retry_after = retry_after_seconds(response.headers)
            wait = (
                f" Groq asked for {retry_after:.0f}s."
                if retry_after is not None
                else ""
            )
            raise LLMRateLimitError(
                "Groq rate limit reached and the retry budget is spent."
                f"{wait} Retry later, lower REPORT_TOP_K, or raise "
                f"GROQ_MIN_INTERVAL_SECONDS to spread the run out."
                f"{_detail(response)}",
                retry_after=retry_after,
            )
        if status in (401, 403):
            raise LLMAuthError(
                f"Groq rejected the configured GROQ_API_KEY.{_detail(response)}"
            )
        if status in (400, 404, 422):
            raise LLMConfigurationError(
                "Groq rejected the request as invalid — check GROQ_MODEL and that "
                f"it supports response_format: json_schema.{_detail(response)}"
            )
        if status >= 500:
            raise LLMUnavailableError(
                f"Groq is unavailable (HTTP {status}). Retry later.{_detail(response)}"
            )
        raise LLMError(f"Groq returned an unexpected HTTP {status}.{_detail(response)}")

    def _parse(self, response: httpx.Response) -> dict:
        try:
            choice = response.json()["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError("Groq returned invalid structured JSON") from exc
        if choice.get("finish_reason") == "length":
            # Distinct from malformed JSON: the model was cut off by our own
            # cap, so the fix is GROQ_MAX_COMPLETION_TOKENS, not the prompt.
            raise LLMError(
                "Groq's response was truncated by max_completion_tokens; raise "
                "GROQ_MAX_COMPLETION_TOKENS."
            )
        try:
            return json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
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
        max_retries=settings.groq_max_retries,
        backoff_base_seconds=settings.groq_backoff_base_seconds,
        backoff_max_seconds=settings.groq_backoff_max_seconds,
        min_interval_seconds=settings.groq_min_interval_seconds,
        max_completion_tokens=settings.groq_max_completion_tokens,
    )
