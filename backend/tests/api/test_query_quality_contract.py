"""`/query` wiring for the scope-aware sufficiency rule and withheld claims."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import routes_backend
from app.api.security import require_api_key
from app.core.config import Settings
from app.interactive.workflow import InteractiveServices

_UNUSED = InteractiveServices(retriever=None, graph_expand=None, llm=None)
SETTINGS = SimpleNamespace(
    retrieval_round_cap=3,
    retrieval_top_n=12,
    retrieval_min_chunks=2,
    retrieval_min_papers=2,
    retrieval_min_papers_paper_specific=1,
    retrieval_pool_factor=3,
    retrieval_min_score=0.2,
    grounding_regeneration_cap=1,
    query_deadline_seconds=30.0,
)
RESULT = {
    "outcome": "grounded_answer",
    "answer": "A.",
    "claims": [{"text": "A.", "evidence_ids": ["e1"]}],
    "citations": [
        {
            "id": "e1",
            "paper_id": 3,
            "title": "T",
            "text": "A.",
            "year": 2026,
            "source_type": "paragraph",
        }
    ],
    "grounded": True,
    "retrieval_rounds": 1,
    "retry_count": 0,
    "regeneration_count": 0,
    "removed_claim_count": 2,
    "trace": [],
    "provider": "groq",
    "model": "m",
}


def _app():
    application = FastAPI()
    application.include_router(routes_backend.router)
    application.dependency_overrides[require_api_key] = lambda: None
    return application


def test_the_route_passes_the_paper_specific_minimum_and_serves_withheld_counts(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(routes_backend, "get_settings", lambda: SETTINGS)
    monkeypatch.setattr(routes_backend, "default_services", lambda _s: _UNUSED)
    monkeypatch.setattr(
        routes_backend, "run_query", lambda *a: calls.append(a) or RESULT
    )
    response = TestClient(_app()).post("/query", json={"question": "What gaps?"})
    assert response.status_code == 200
    assert response.json()["removed_claim_count"] == 2
    assert calls[0][2:] == (3, 12, 2, 2, 0.2, 1, 1, 3)


def test_a_result_without_the_count_is_served_with_zero(monkeypatch):
    legacy = {k: v for k, v in RESULT.items() if k != "removed_claim_count"}
    monkeypatch.setattr(routes_backend, "get_settings", lambda: SETTINGS)
    monkeypatch.setattr(routes_backend, "default_services", lambda _s: _UNUSED)
    monkeypatch.setattr(routes_backend, "run_query", lambda *a: legacy)
    response = TestClient(_app()).post("/query", json={"question": "What gaps?"})
    assert response.json()["removed_claim_count"] == 0


def test_openapi_documents_the_withheld_claim_count():
    schema = _app().openapi()["components"]["schemas"]["QueryResponse"]
    assert schema["properties"]["removed_claim_count"]["type"] == "integer"


def test_the_paper_specific_minimum_defaults_to_one_and_is_bounded(monkeypatch):
    from tests.core.test_config import _set_all_required

    _set_all_required(monkeypatch)
    monkeypatch.delenv("RETRIEVAL_MIN_PAPERS_PAPER_SPECIFIC", raising=False)
    assert Settings(_env_file=None).retrieval_min_papers_paper_specific == 1
    with pytest.raises(ValidationError):
        Settings(_env_file=None, retrieval_min_papers_paper_specific=0)


def test_env_example_documents_the_paper_specific_minimum():
    from pathlib import Path

    example = (Path(__file__).resolve().parents[3] / ".env.example").read_text(
        encoding="utf-8"
    )
    assert "RETRIEVAL_MIN_PAPERS_PAPER_SPECIFIC=1" in example
