"""Tests for backend/app/core/config.py — centralized pydantic-settings Settings.

Covers D-07 (config centralization) and D-09 (the full 14-variable .env contract).
"""

import pytest

ALL_REQUIRED_ENV = {
    "NEO4J_URI": "bolt://neo4j:7687",
    "NEO4J_USER": "neo4j",
    "NEO4J_PASSWORD": "test_neo4j_pw",
    "QDRANT_URL": "http://qdrant:6333",
    "POSTGRES_HOST": "postgres",
    "POSTGRES_PORT": "5432",
    "POSTGRES_USER": "scholarlens",
    "POSTGRES_PASSWORD": "test_pg_pw",
    "POSTGRES_DB": "scholarlens",
    "PDF_STORAGE_PATH": "/data/pdfs",
    "MAX_UPLOAD_SIZE_MB": "50",
    "UNPAYWALL_EMAIL": "you@example.com",
}


def _set_all_required(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in ALL_REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def test_settings_loads_all_d09_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings exposes all D-09 fields matching the env values exactly."""
    _set_all_required(monkeypatch)
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "s2-test-key")
    monkeypatch.setenv("LLM_API_KEY", "llm-test-key")

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.neo4j_uri == "bolt://neo4j:7687"
    assert settings.neo4j_user == "neo4j"
    assert settings.neo4j_password == "test_neo4j_pw"
    assert settings.qdrant_url == "http://qdrant:6333"
    assert settings.postgres_host == "postgres"
    assert settings.postgres_port == 5432
    assert isinstance(settings.postgres_port, int)
    assert settings.postgres_user == "scholarlens"
    assert settings.postgres_password == "test_pg_pw"
    assert settings.postgres_db == "scholarlens"
    assert settings.pdf_storage_path == "/data/pdfs"
    assert settings.max_upload_size_mb == 50
    assert isinstance(settings.max_upload_size_mb, int)
    assert settings.unpaywall_email == "you@example.com"
    assert settings.semantic_scholar_api_key == "s2-test-key"
    assert settings.llm_api_key == "llm-test-key"


def test_optional_keys_default_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """semantic_scholar_api_key and llm_api_key default to None when unset."""
    _set_all_required(monkeypatch)
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.semantic_scholar_api_key is None
    assert settings.llm_api_key is None


def test_postgres_url_computed_property(monkeypatch: pytest.MonkeyPatch) -> None:
    """postgres_url builds a valid SQLAlchemy psycopg2 connection string."""
    _set_all_required(monkeypatch)

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert (
        settings.postgres_url
        == "postgresql+psycopg2://scholarlens:test_pg_pw@postgres:5432/scholarlens"
    )


def test_get_settings_returns_settings_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_settings() is the module-level accessor other modules should import."""
    _set_all_required(monkeypatch)

    from app.core.config import Settings, get_settings

    result = get_settings()

    assert isinstance(result, Settings)


def test_acl_anthology_bib_path_defaults_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """acl_anthology_bib_path is optional — existing .env files need no change."""
    _set_all_required(monkeypatch)
    monkeypatch.delenv("ACL_ANTHOLOGY_BIB_PATH", raising=False)

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.acl_anthology_bib_path == "data/acl_anthology_export.bib"
