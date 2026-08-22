"""Unit tests for backend/app/core/health.py — per-service round-trip checks.

Covers D-06's requirement that each check does a real round-trip but never
propagates a connection exception to the caller.
"""

from app.core import health
from app.core.config import Settings


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        neo4j_uri="bolt://neo4j:7687",
        neo4j_user="neo4j",
        neo4j_password="pw",
        qdrant_url="http://qdrant:6333",
        postgres_host="postgres",
        postgres_port=5432,
        postgres_user="scholarlens",
        postgres_password="pw",
        postgres_db="scholarlens",
        pdf_storage_path="/data/pdfs",
        max_upload_size_mb=50,
        unpaywall_email="you@example.com",
    )


def test_check_neo4j_returns_false_on_connection_error(monkeypatch) -> None:
    def _raise_driver(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(health.GraphDatabase, "driver", _raise_driver)

    assert health.check_neo4j(_settings()) is False


def test_check_qdrant_returns_false_on_connection_error(monkeypatch) -> None:
    def _raise_client(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(health, "QdrantClient", _raise_client)

    assert health.check_qdrant(_settings()) is False


def test_check_postgres_returns_false_on_connection_error(monkeypatch) -> None:
    def _raise_engine(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(health, "create_engine", _raise_engine)

    assert health.check_postgres(_settings()) is False
