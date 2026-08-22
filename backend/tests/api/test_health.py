"""Tests for GET /health — the D-06 response contract.

check_neo4j/check_qdrant/check_postgres are monkeypatched at their import
site in app.api.routes_health so these tests exercise routing/response-shape
logic only, not real service connections.
"""

import pytest
from fastapi.testclient import TestClient

from app.api import routes_health

REQUIRED_ENV = {
    "NEO4J_URI": "bolt://neo4j:7687",
    "NEO4J_USER": "neo4j",
    "NEO4J_PASSWORD": "pw",
    "QDRANT_URL": "http://qdrant:6333",
    "POSTGRES_HOST": "postgres",
    "POSTGRES_PORT": "5432",
    "POSTGRES_USER": "scholarlens",
    "POSTGRES_PASSWORD": "pw",
    "POSTGRES_DB": "scholarlens",
    "PDF_STORAGE_PATH": "/data/pdfs",
    "MAX_UPLOAD_SIZE_MB": "50",
    "UNPAYWALL_EMAIL": "you@example.com",
    "GROBID_URL": "http://grobid:8070",
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture()
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


def test_health_returns_200_when_all_services_up(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_health, "check_neo4j", lambda settings: True)
    monkeypatch.setattr(routes_health, "check_qdrant", lambda settings: True)
    monkeypatch.setattr(routes_health, "check_postgres", lambda settings: True)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "services": {"neo4j": True, "qdrant": True, "postgres": True},
    }


def test_health_returns_503_when_one_service_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_health, "check_neo4j", lambda settings: True)
    monkeypatch.setattr(routes_health, "check_qdrant", lambda settings: True)
    monkeypatch.setattr(routes_health, "check_postgres", lambda settings: False)

    response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["services"]["postgres"] is False


def test_health_returns_503_when_all_services_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_health, "check_neo4j", lambda settings: False)
    monkeypatch.setattr(routes_health, "check_qdrant", lambda settings: False)
    monkeypatch.setattr(routes_health, "check_postgres", lambda settings: False)

    response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["services"] == {"neo4j": False, "qdrant": False, "postgres": False}
