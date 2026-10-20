from types import SimpleNamespace
from unittest.mock import ANY

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import routes_backend
from app.api.security import require_api_key
from app.explanation.llm import LLMUnavailableError
from app.interactive.workflow import InteractiveServices

# `/query` wraps its services with the overall deadline guard; tests that
# replace `run_query` never call them, so any InteractiveServices will do.
_UNUSED_SERVICES = InteractiveServices(retriever=None, graph_expand=None, llm=None)


def test_query_returns_service_unavailable_when_llm_provider_is_offline(monkeypatch):
    monkeypatch.setattr(
        routes_backend,
        "get_settings",
        lambda: SimpleNamespace(
            retrieval_round_cap=3,
            retrieval_top_n=12,
            retrieval_min_chunks=2,
            retrieval_min_papers=2,
            retrieval_min_papers_paper_specific=1,
            retrieval_pool_factor=1,
            retrieval_min_score=0.2,
            grounding_regeneration_cap=1,
            query_deadline_seconds=30.0,
        ),
    )
    monkeypatch.setattr(
        routes_backend, "default_services", lambda settings: _UNUSED_SERVICES
    )
    monkeypatch.setattr(
        routes_backend,
        "run_query",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            LLMUnavailableError("Groq is unavailable. Check network access.")
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        routes_backend.query(routes_backend.QueryRequest(question="What gaps exist?"))

    assert exc_info.value.status_code == 503
    assert "Groq is unavailable" in exc_info.value.detail


def test_query_endpoint_accepts_a_natural_language_question(monkeypatch):
    application = FastAPI()
    application.include_router(routes_backend.router)
    application.dependency_overrides[require_api_key] = lambda: None
    expected = {
        "outcome": "grounded_answer",
        "answer": "Multilingual evaluation remains limited [e1].",
        "claims": [
            {
                "text": "Multilingual evaluation remains limited.",
                "evidence_ids": ["e1"],
            }
        ],
        "citations": [
            {
                "id": "e1",
                "paper_id": 7,
                "title": "A paper",
                "text": "Evaluation is English-only.",
                "year": 2024,
                "source_type": "limitation",
            }
        ],
        "grounded": True,
        "retrieval_rounds": 2,
        "retry_count": 1,
        "regeneration_count": 0,
        "removed_claim_count": 0,
        "trace": ["retrieve:round=1", "reformulate", "retrieve:round=2"],
        "provider": "groq",
        "model": "groq-test",
    }
    settings = SimpleNamespace(
        retrieval_round_cap=3,
        retrieval_top_n=12,
        retrieval_min_chunks=2,
        retrieval_min_papers=2,
        retrieval_min_papers_paper_specific=1,
        retrieval_pool_factor=1,
        retrieval_min_score=0.2,
        grounding_regeneration_cap=1,
        query_deadline_seconds=30.0,
    )
    monkeypatch.setattr(routes_backend, "get_settings", lambda: settings)
    monkeypatch.setattr(
        routes_backend, "default_services", lambda _settings: _UNUSED_SERVICES
    )
    calls = []

    def fake_run_query(*args):
        calls.append(args)
        return expected

    monkeypatch.setattr(routes_backend, "run_query", fake_run_query)

    response = TestClient(application).post(
        "/query", json={"question": "What gaps remain in multilingual evaluation?"}
    )

    assert response.status_code == 200
    assert response.json() == expected
    assert calls == [
        (
            "What gaps remain in multilingual evaluation?",
            ANY,
            3,
            12,
            2,
            2,
            0.2,
            1,
            1,
            1,
        )
    ]


@pytest.mark.parametrize("question", ["", "  ", "x" * 2001])
def test_query_endpoint_rejects_blank_or_oversized_input(question):
    application = FastAPI()
    application.include_router(routes_backend.router)
    application.dependency_overrides[require_api_key] = lambda: None

    response = TestClient(application).post("/query", json={"question": question})

    assert response.status_code == 422


def _app():
    application = FastAPI()
    application.include_router(routes_backend.router)
    application.dependency_overrides[require_api_key] = lambda: None
    return application


def test_openapi_documents_the_query_outcome_and_citation_shape():
    schema = _app().openapi()
    ref = schema["paths"]["/query"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    model = schema["components"]["schemas"][ref.rsplit("/", 1)[1]]
    assert set(model["properties"]["outcome"]["enum"]) == {
        "grounded_answer",
        "no_indexed_evidence",
        "insufficient_evidence",
        "grounding_failed",
    }
    assert "outcome" in model["required"] and "citations" in model["required"]


def test_a_workflow_result_with_an_unknown_outcome_is_not_served(monkeypatch):
    settings = SimpleNamespace(
        retrieval_round_cap=3,
        retrieval_top_n=12,
        retrieval_min_chunks=2,
        retrieval_min_papers=2,
        retrieval_min_papers_paper_specific=1,
        retrieval_pool_factor=1,
        retrieval_min_score=0.2,
        grounding_regeneration_cap=1,
        query_deadline_seconds=30.0,
    )
    monkeypatch.setattr(routes_backend, "get_settings", lambda: settings)
    monkeypatch.setattr(routes_backend, "default_services", lambda _s: _UNUSED_SERVICES)
    bogus = {
        "outcome": "probably_fine",
        "answer": "x",
        "claims": [],
        "citations": [],
        "grounded": True,
        "retrieval_rounds": 1,
        "retry_count": 0,
        "regeneration_count": 0,
        "trace": [],
        "provider": "groq",
        "model": "m",
    }
    monkeypatch.setattr(routes_backend, "run_query", lambda *_a: bogus)
    response = TestClient(_app(), raise_server_exceptions=False).post(
        "/query", json={"question": "What gaps remain?"}
    )
    assert response.status_code >= 500


@pytest.mark.parametrize(
    "body",
    [b"{not json", b'{"question": 42}', b"[]", b'{"q": "Which languages?"}'],
)
def test_a_malformed_or_mistyped_body_is_a_422_that_runs_nothing(monkeypatch, body):
    def never(*_a, **_k):
        raise AssertionError("the workflow ran on an invalid request")

    monkeypatch.setattr(routes_backend, "run_query", never)
    response = TestClient(_app()).post(
        "/query", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
