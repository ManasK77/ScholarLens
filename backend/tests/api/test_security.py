"""Tests for backend/app/api/security.py — Phase 11 API-key authentication.

The contract these tests pin down (README §12 shows the client side of it —
`curl -H "X-API-Key: YOUR_API_KEY"`, and frontend/src/api.ts sends the same
header and reads `{"detail": ...}` off failures):

* header name is `X-API-Key` (HTTP header matching is case-insensitive)
* a request with no key, an empty key, or a wrong key is rejected with 401
* a request carrying the configured key is accepted
* the dependency **fails closed**: when the server has no `API_KEY`
  configured, every authenticated request is rejected rather than allowed
* rejection details never echo the configured key
* `GET /health` stays unauthenticated so container health probes keep working
"""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api import routes_ingestion
from app.api.security import API_KEY_HEADER_NAME, require_api_key
from app.core.config import Settings
from app.ingestion import corpus
from app.ingestion.sources import acl_anthology, arxiv, semantic_scholar
from app.main import app

VALID_KEY = "test-api-key-2f8c1d"

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


def _settings(monkeypatch: pytest.MonkeyPatch, api_key: str | None) -> Settings:
    """A real Settings instance (not a stub) so field-name drift fails loudly."""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    if api_key is None:
        monkeypatch.delenv("API_KEY", raising=False)
    else:
        monkeypatch.setenv("API_KEY", api_key)
    return Settings(_env_file=None)


# --------------------------------------------------------------------------
# require_api_key — unit level
# --------------------------------------------------------------------------


def test_accepts_the_configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, VALID_KEY)

    assert require_api_key(VALID_KEY, settings) is None


def test_rejects_missing_key_with_401(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, VALID_KEY)

    with pytest.raises(HTTPException) as exc_info:
        require_api_key(None, settings)

    assert exc_info.value.status_code == 401


def test_rejects_empty_key_with_401(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, VALID_KEY)

    with pytest.raises(HTTPException) as exc_info:
        require_api_key("", settings)

    assert exc_info.value.status_code == 401


def test_rejects_incorrect_key_with_401(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, VALID_KEY)

    with pytest.raises(HTTPException) as exc_info:
        require_api_key("not-the-key", settings)

    assert exc_info.value.status_code == 401


def test_rejects_key_that_is_a_prefix_of_the_configured_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards against a truncating/`startswith` comparison."""
    settings = _settings(monkeypatch, VALID_KEY)

    with pytest.raises(HTTPException) as exc_info:
        require_api_key(VALID_KEY[:-1], settings)

    assert exc_info.value.status_code == 401


def test_rejects_non_ascii_key_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`secrets.compare_digest` raises TypeError on non-ASCII str operands.

    A caller-supplied header must never be able to turn a 401 into a 500.
    """
    settings = _settings(monkeypatch, VALID_KEY)

    with pytest.raises(HTTPException) as exc_info:
        require_api_key("kéy-with-nön-ascii", settings)

    assert exc_info.value.status_code == 401


def test_fails_closed_when_no_key_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No configured key must reject everything — never authenticate everyone."""
    settings = _settings(monkeypatch, None)

    with pytest.raises(HTTPException) as exc_info:
        require_api_key(VALID_KEY, settings)

    assert exc_info.value.status_code == 500


def test_fails_closed_when_configured_key_is_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`API_KEY=` / `API_KEY="   "` in .env must not disable authentication."""
    settings = _settings(monkeypatch, "   ")

    with pytest.raises(HTTPException) as exc_info:
        require_api_key("   ", settings)

    assert exc_info.value.status_code == 500


def test_blank_configured_key_is_not_matchable_by_an_empty_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, "")

    with pytest.raises(HTTPException) as exc_info:
        require_api_key("", settings)

    assert exc_info.value.status_code == 500


def test_tolerates_trailing_whitespace_in_the_configured_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CRLF-terminated `.env` line must not lock the owner out."""
    settings = _settings(monkeypatch, f"  {VALID_KEY}  ")

    assert require_api_key(VALID_KEY, settings) is None


def test_rejection_detail_never_echoes_the_configured_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, VALID_KEY)

    with pytest.raises(HTTPException) as exc_info:
        require_api_key("wrong", settings)

    assert VALID_KEY not in str(exc_info.value.detail)
    assert "wrong" not in str(exc_info.value.detail)


def test_misconfiguration_detail_never_echoes_the_presented_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, None)

    with pytest.raises(HTTPException) as exc_info:
        require_api_key("presented-secret", settings)

    assert "presented-secret" not in str(exc_info.value.detail)


# --------------------------------------------------------------------------
# Wiring — the protected surface as mounted on the app
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _app_env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PDF_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("API_KEY", VALID_KEY)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    """A client whose /collect run touches no network and no real database."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.ingestion.corpus import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    def _override_get_session():
        yield session

    monkeypatch.setattr(arxiv, "search_topic", lambda topic, **kwargs: [])
    monkeypatch.setattr(
        semantic_scholar, "search_topic", lambda topic, api_key=None, **kwargs: []
    )
    monkeypatch.setattr(
        acl_anthology,
        "download_anthology_export",
        lambda dest_path, force=False, **kw: None,
    )
    monkeypatch.setattr(acl_anthology, "search_topic", lambda bib_path, topic: [])
    monkeypatch.setattr(
        routes_ingestion.unpaywall, "enrich", lambda session, email, dest_dir, **kw: []
    )

    app.dependency_overrides[corpus.get_session] = _override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()
    session.close()


def test_collect_rejects_request_without_api_key(client: TestClient) -> None:
    response = client.post("/collect", json={"topic": "anything"})

    assert response.status_code == 401
    assert "detail" in response.json()


def test_collect_rejects_request_with_wrong_api_key(client: TestClient) -> None:
    response = client.post(
        "/collect",
        json={"topic": "anything"},
        headers={API_KEY_HEADER_NAME: "wrong-key"},
    )

    assert response.status_code == 401


def test_collect_accepts_request_with_correct_api_key(client: TestClient) -> None:
    response = client.post(
        "/collect",
        json={"topic": "anything"},
        headers={API_KEY_HEADER_NAME: VALID_KEY},
    )

    assert response.status_code == 200


def test_api_key_header_name_is_matched_case_insensitively(
    client: TestClient,
) -> None:
    response = client.post(
        "/collect",
        json={"topic": "anything"},
        headers={"x-api-key": VALID_KEY},
    )

    assert response.status_code == 200


def test_authentication_runs_before_request_body_validation(
    client: TestClient,
) -> None:
    """An unauthenticated caller must not learn the request schema from a 422."""
    response = client.post("/collect", json={"not_the_topic_field": 1})

    assert response.status_code == 401


def test_upload_rejects_request_without_api_key(client: TestClient) -> None:
    response = client.post(
        "/upload",
        files={"files": ("paper.pdf", b"%PDF-1.4 fake content", "application/pdf")},
    )

    assert response.status_code == 401


def test_upload_accepts_request_with_correct_api_key(client: TestClient) -> None:
    response = client.post(
        "/upload",
        files={"files": ("paper.pdf", b"%PDF-1.4 fake content", "application/pdf")},
        headers={API_KEY_HEADER_NAME: VALID_KEY},
    )

    assert response.status_code == 200


def test_health_stays_unauthenticated(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Container/orchestrator health probes carry no credential."""
    from app.api import routes_health

    monkeypatch.setattr(routes_health, "check_neo4j", lambda settings: True)
    monkeypatch.setattr(routes_health, "check_qdrant", lambda settings: True)
    monkeypatch.setattr(routes_health, "check_postgres", lambda settings: True)

    response = client.get("/health")

    assert response.status_code == 200


def test_protected_routes_are_rejected_when_server_key_is_unset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("API_KEY", raising=False)

    response = client.post(
        "/collect",
        json={"topic": "anything"},
        headers={API_KEY_HEADER_NAME: VALID_KEY},
    )

    assert response.status_code == 500


# --------------------------------------------------------------------------
# OpenAPI surface
# --------------------------------------------------------------------------


def test_openapi_declares_the_api_key_security_scheme(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]

    assert any(
        scheme.get("in") == "header" and scheme.get("name") == API_KEY_HEADER_NAME
        for scheme in schemes.values()
    )


def test_openapi_marks_protected_routes_and_leaves_health_open(
    client: TestClient,
) -> None:
    schema = client.get("/openapi.json").json()

    assert schema["paths"]["/collect"]["post"].get("security")
    assert schema["paths"]["/upload"]["post"].get("security")
    assert not schema["paths"]["/health"]["get"].get("security")
