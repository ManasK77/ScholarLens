from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import routes_backend
from app.explanation.llm import LLMUnavailableError


def test_query_returns_service_unavailable_when_llm_provider_is_offline(monkeypatch):
    monkeypatch.setattr(
        routes_backend,
        "get_settings",
        lambda: SimpleNamespace(retrieval_round_cap=3, retrieval_top_n=12),
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
