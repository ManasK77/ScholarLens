"""The overall `/query` deadline (Phase 11 hardening addendum).

Every dependency is a fake: no Groq, no Qdrant, no Neo4j, no model. "Slow"
dependencies block on an `Event` the test controls, so the tests are bounded
by the deadline under test rather than by real sleeps of the fake.
"""

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_backend
from app.api.security import require_api_key
from app.interactive import deadline as deadline_module
from app.interactive.workflow import InteractiveServices
from tests.interactive.test_workflow import (
    FakeGraphExpander,
    FakeRetriever,
    ScriptedLLM,
    _answer,
    _point,
)

DEADLINE = 0.5
# Generous for a loaded CI machine, but far below the fakes' 30 s block.
RESPONSE_BOUND = 5.0


def _settings(deadline_seconds: float = DEADLINE) -> SimpleNamespace:
    return SimpleNamespace(
        retrieval_round_cap=3,
        retrieval_top_n=12,
        retrieval_min_chunks=2,
        retrieval_min_papers=2,
        retrieval_min_papers_paper_specific=1,
        retrieval_pool_factor=1,
        retrieval_min_score=0.2,
        grounding_regeneration_cap=1,
        query_deadline_seconds=deadline_seconds,
    )


def _grounded_llm() -> ScriptedLLM:
    return ScriptedLLM(
        answer=[_answer("e1", "e2")],
        verify=[{"grounded": True, "unsupported_claim_indexes": []}],
    )


def _retriever() -> FakeRetriever:
    return FakeRetriever([[_point("e1", 8), _point("e2", 9)]])


class _BlockingLLM(ScriptedLLM):
    """Blocks the first generation until released, then answers normally."""

    def __init__(self) -> None:
        super().__init__(
            answer=[_answer("e1", "e2"), _answer("e1", "e2")],
            verify=[{"grounded": True, "unsupported_claim_indexes": []}] * 2,
        )
        self.release = threading.Event()
        self.started = threading.Event()
        self.finished = threading.Event()
        self.calls = 0

    def generate_json(self, prompt: str, schema: dict) -> dict:
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            self.release.wait(timeout=30)
            self.finished.set()
        return super().generate_json(prompt, schema)


class _BlockingRetriever(FakeRetriever):
    def __init__(self) -> None:
        super().__init__([[_point("e1", 8), _point("e2", 9)]])
        self.release = threading.Event()

    def retrieve(self, query: str, limit: int) -> list[dict]:
        self.release.wait(timeout=30)
        return super().retrieve(query, limit)


@pytest.fixture()
def client():
    application = FastAPI()
    application.include_router(routes_backend.router)
    application.dependency_overrides[require_api_key] = lambda: None
    return TestClient(application, raise_server_exceptions=False)


def _use(monkeypatch, services: InteractiveServices, deadline_seconds=DEADLINE):
    monkeypatch.setattr(
        routes_backend, "get_settings", lambda: _settings(deadline_seconds)
    )
    monkeypatch.setattr(routes_backend, "default_services", lambda settings: services)


def _post(client: TestClient):
    started = time.monotonic()
    response = client.post("/query", json={"question": "What gaps exist?"})
    return response, time.monotonic() - started


def test_a_query_within_the_deadline_returns_the_normal_grounded_answer(
    client, monkeypatch
) -> None:
    llm = _grounded_llm()
    _use(monkeypatch, InteractiveServices(_retriever(), FakeGraphExpander(), llm))

    response, _ = _post(client)

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "grounded_answer"
    assert body["grounded"] is True
    assert [c["id"] for c in body["citations"]] == ["e1", "e2"]
    assert body["provider"] == "groq" and body["model"] == "groq-test"


def test_a_slow_model_call_ends_the_request_with_a_safe_504(
    client, monkeypatch
) -> None:
    llm = _BlockingLLM()
    _use(monkeypatch, InteractiveServices(_retriever(), FakeGraphExpander(), llm))

    try:
        response, elapsed = _post(client)
    finally:
        llm.release.set()

    assert response.status_code == 504
    assert elapsed < RESPONSE_BOUND
    assert response.json() == {
        "detail": (
            "The question was not answered within the 0.5-second limit. "
            "No answer was produced; try a narrower question or retry later."
        )
    }


def test_no_further_model_call_starts_after_the_deadline(client, monkeypatch) -> None:
    """The blocked call finishes in the background; the grounding check that
    would follow it must not run, and no regeneration either."""
    llm = _BlockingLLM()
    _use(monkeypatch, InteractiveServices(_retriever(), FakeGraphExpander(), llm))

    response, _ = _post(client)
    llm.release.set()
    assert llm.finished.wait(timeout=RESPONSE_BOUND)
    # Give the abandoned worker time to reach its next (guarded) call.
    time.sleep(0.2)

    assert response.status_code == 504
    assert llm.calls == 1
    assert llm.prompts["verify"] == []


def test_a_slow_retrieval_also_hits_the_deadline(client, monkeypatch) -> None:
    retriever = _BlockingRetriever()
    llm = _grounded_llm()
    _use(monkeypatch, InteractiveServices(retriever, FakeGraphExpander(), llm))

    try:
        response, elapsed = _post(client)
    finally:
        retriever.release.set()

    assert response.status_code == 504
    assert elapsed < RESPONSE_BOUND
    time.sleep(0.2)
    assert llm.prompts["answer"] == []


def test_a_timed_out_query_does_not_break_the_next_one(client, monkeypatch) -> None:
    slow = _BlockingLLM()
    _use(monkeypatch, InteractiveServices(_retriever(), FakeGraphExpander(), slow))
    first, _ = _post(client)
    slow.release.set()

    _use(
        monkeypatch,
        InteractiveServices(_retriever(), FakeGraphExpander(), _grounded_llm()),
    )
    second, _ = _post(client)

    assert first.status_code == 504
    assert second.status_code == 200
    assert second.json()["outcome"] == "grounded_answer"


def test_a_timeout_never_reports_a_grounded_answer_or_internal_detail(
    client, monkeypatch, caplog
) -> None:
    llm = _BlockingLLM()
    _use(monkeypatch, InteractiveServices(_retriever(), FakeGraphExpander(), llm))

    try:
        response, _ = _post(client)
    finally:
        llm.release.set()

    text = response.text
    assert "grounded_answer" not in text
    assert "Traceback" not in text and "QueryDeadlineExceeded" not in text
    assert "What gaps exist?" not in caplog.text


def test_errors_inside_the_deadline_keep_their_existing_mapping(
    client, monkeypatch
) -> None:
    from app.explanation.llm import LLMUnavailableError

    class _Down(ScriptedLLM):
        def generate_json(self, prompt, schema):
            raise LLMUnavailableError("Groq is unavailable. Check network access.")

    _use(monkeypatch, InteractiveServices(_retriever(), FakeGraphExpander(), _Down()))

    response, _ = _post(client)

    assert response.status_code == 503
    assert "Groq is unavailable" in response.json()["detail"]


# --------------------------------------------------------------------------
# The deadline primitives
# --------------------------------------------------------------------------


def test_deadline_uses_an_injectable_clock() -> None:
    now = [100.0]
    deadline = deadline_module.Deadline(10.0, clock=lambda: now[0])

    assert deadline.remaining() == pytest.approx(10.0)
    deadline.check("retrieve")
    now[0] = 110.5
    assert deadline.remaining() == 0.0
    with pytest.raises(deadline_module.QueryDeadlineExceeded):
        deadline.check("generate")


def test_guarded_services_delegate_everything_they_do_not_guard() -> None:
    retriever = _retriever()
    guarded = deadline_module.bound_services(
        InteractiveServices(retriever, FakeGraphExpander(), _grounded_llm()),
        deadline_module.Deadline(60.0),
    )

    assert guarded.llm.provider == "groq"
    assert guarded.retriever.has_evidence() is True
    assert guarded.reranker is None and guarded.title_lookup is None


def test_the_deadline_setting_is_positive_with_a_documented_default(
    monkeypatch,
) -> None:
    from pydantic import ValidationError

    from app.core.config import Settings

    for key, value in {
        "NEO4J_URI": "bolt://n:7687",
        "NEO4J_USER": "n",
        "NEO4J_PASSWORD": "p",
        "QDRANT_URL": "http://q:6333",
        "POSTGRES_HOST": "p",
        "POSTGRES_PORT": "5432",
        "POSTGRES_USER": "u",
        "POSTGRES_PASSWORD": "p",
        "POSTGRES_DB": "d",
        "PDF_STORAGE_PATH": "/tmp",
        "MAX_UPLOAD_SIZE_MB": "5",
        "UNPAYWALL_EMAIL": "a@example.com",
        "GROBID_URL": "http://g:8070",
    }.items():
        monkeypatch.setenv(key, value)

    assert Settings(_env_file=None).query_deadline_seconds == 180.0
    monkeypatch.setenv("QUERY_DEADLINE_SECONDS", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# --- the pacing wait counts against the deadline (D-B-04) ---------------------------


def test_a_paced_model_call_that_cannot_start_in_time_is_never_sent() -> None:
    """The 2026-10-18 live incident: GROQ_MIN_INTERVAL_SECONDS=60 queued the
    second verification behind the deadline; it was sent 12 s after the 504."""
    from app.explanation.llm import GroqClient
    from app.ingestion.rate_limit import RateLimiter
    from tests.ingestion.fake_clock import FakeClock

    fake = FakeClock()
    sent = []

    class Http:
        def post(self, url, headers, json):
            sent.append(url)
            raise AssertionError("a request was sent past the deadline")

    llm = GroqClient(
        "k",
        "m",
        http_client=Http(),
        limiter=RateLimiter(60.0, clock=fake.clock, sleep=fake.sleep),
    )
    llm.limiter.acquire()  # the previous call, at t=0
    fake.now = 10.0  # 50 s until the next slot
    deadline = deadline_module.Deadline(30.0, clock=fake.clock)
    services = deadline_module.bound_services(
        InteractiveServices(_retriever(), FakeGraphExpander(), llm), deadline
    )
    with pytest.raises(deadline_module.QueryDeadlineExceeded, match="pacing"):
        services.llm.generate_json("TASK: ANSWER", {})
    assert sent == [] and fake.sleeps == []


def test_a_paced_call_that_fits_the_deadline_still_runs() -> None:
    from app.explanation.llm import GroqClient
    from app.ingestion.rate_limit import RateLimiter
    from tests.ingestion.fake_clock import FakeClock

    fake = FakeClock()

    class Http:
        def post(self, url, headers, json):
            return SimpleNamespace(
                status_code=200,
                headers={},
                json=lambda: {
                    "choices": [
                        {
                            "message": {"content": '{"ok": true}'},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )

    llm = GroqClient(
        "k",
        "m",
        http_client=Http(),
        limiter=RateLimiter(60.0, clock=fake.clock, sleep=fake.sleep),
    )
    llm.limiter.acquire()
    fake.now = 10.0
    deadline = deadline_module.Deadline(120.0, clock=fake.clock)
    services = deadline_module.bound_services(
        InteractiveServices(_retriever(), FakeGraphExpander(), llm), deadline
    )
    assert services.llm.generate_json("TASK: ANSWER", {"type": "object"}) == {
        "ok": True
    }
    assert fake.sleeps == [pytest.approx(50.0)]


def test_mode_a_clients_keep_no_wait_budget() -> None:
    from app.explanation.llm import GroqClient

    assert GroqClient("k", "m").wait_budget is None
