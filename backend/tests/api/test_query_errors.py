from types import SimpleNamespace
from unittest.mock import ANY

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import routes_backend
from app.api.security import require_api_key
from app.explanation.llm import LLMUnavailableError


def test_query_returns_service_unavailable_when_llm_provider_is_offline(monkeypatch):
    monkeypatch.setattr(
        routes_backend,
        "get_settings",
        lambda: SimpleNamespace(
            retrieval_round_cap=3,
            retrieval_top_n=12,
            retrieval_min_chunks=2,
            retrieval_min_papers=2,
            retrieval_min_score=0.2,
            grounding_regeneration_cap=1,
        ),
    )
    monkeypatch.setattr(routes_backend, "default_services", lambda settings: object())
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
        "trace": ["retrieve:round=1", "reformulate", "retrieve:round=2"],
        "provider": "groq",
        "model": "groq-test",
    }
    settings = SimpleNamespace(
        retrieval_round_cap=3,
        retrieval_top_n=12,
        retrieval_min_chunks=2,
        retrieval_min_papers=2,
        retrieval_min_score=0.2,
        grounding_regeneration_cap=1,
    )
    monkeypatch.setattr(routes_backend, "get_settings", lambda: settings)
    monkeypatch.setattr(routes_backend, "default_services", lambda _settings: object())
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
        )
    ]


@pytest.mark.parametrize("question", ["", "  ", "x" * 2001])
def test_query_endpoint_rejects_blank_or_oversized_input(question):
    application = FastAPI()
    application.include_router(routes_backend.router)
    application.dependency_overrides[require_api_key] = lambda: None

    response = TestClient(application).post("/query", json={"question": question})

    assert response.status_code == 422
