"""Tests for backend/app/api/routes_ingestion.py — /collect and /upload (D-20, D-21)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import routes_ingestion
from app.ingestion import corpus
from app.ingestion.corpus import Base, Paper
from app.ingestion.sources import acl_anthology, arxiv, semantic_scholar
from app.main import app

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
}


@pytest.fixture(autouse=True)
def _settings_env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PDF_STORAGE_PATH", str(tmp_path))


@pytest.fixture()
def test_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    yield session
    session.close()


@pytest.fixture()
def client(test_session: Session):
    def _override_get_session():
        yield test_session

    app.dependency_overrides[corpus.get_session] = _override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_collect_orchestrates_sources_dedups_and_enriches(
    client: TestClient, test_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    arxiv_candidate = {
        "title": "Graph Neural Networks",
        "authors": ["A"],
        "year": 2023,
        "venue": None,
        "doi": "10.1/shared",
        "abstract": "abstract",
        "arxiv_id": "2301.00001",
        "pdf_url": "https://arxiv.org/pdf/2301.00001",
        "source": "arxiv",
    }
    s2_candidate = {
        "title": "Graph Neural Networks",
        "authors": ["A"],
        "year": 2023,
        "venue": "NeurIPS",
        "doi": "10.1/shared",
        "abstract": "abstract",
        "openaccess_pdf_url": None,
    }
    acl_candidate = {
        "title": "Unrelated ACL Paper",
        "authors": ["B"],
        "year": 2022,
        "venue": "ACL",
        "doi": None,
        "abstract": "abstract",
        "acl_id": "2022.acl-1.1",
        "pdf_url": "https://aclanthology.org/2022.acl-1.1.pdf",
    }

    monkeypatch.setattr(arxiv, "search_topic", lambda topic: [arxiv_candidate])
    monkeypatch.setattr(arxiv, "fetch_pdf", lambda candidate, dest_dir: None)
    monkeypatch.setattr(
        semantic_scholar, "search_topic", lambda topic, api_key=None: [s2_candidate]
    )
    monkeypatch.setattr(
        acl_anthology, "download_anthology_export", lambda dest_path, force=False: None
    )
    monkeypatch.setattr(
        acl_anthology, "search_topic", lambda bib_path, topic: [acl_candidate]
    )
    monkeypatch.setattr(acl_anthology, "fetch_pdf", lambda candidate, dest_dir: None)

    def fake_enrich(session, email, dest_dir):
        return []

    monkeypatch.setattr(routes_ingestion.unpaywall, "enrich", fake_enrich)

    response = client.post("/collect", json={"topic": "graph neural networks"})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2  # DOI-duplicate merges into one row + the ACL candidate
    assert len(body["ids"]) == 2

    all_papers = test_session.query(Paper).all()
    shared_doi_rows = [p for p in all_papers if p.doi == "10.1/shared"]
    assert len(shared_doi_rows) == 1  # dedup ran


def test_collect_includes_enriched_ids(
    client: TestClient, test_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(arxiv, "search_topic", lambda topic: [])
    monkeypatch.setattr(
        semantic_scholar, "search_topic", lambda topic, api_key=None: []
    )
    monkeypatch.setattr(
        acl_anthology, "download_anthology_export", lambda dest_path, force=False: None
    )
    monkeypatch.setattr(acl_anthology, "search_topic", lambda bib_path, topic: [])

    def fake_enrich(session, email, dest_dir):
        return [42]

    monkeypatch.setattr(routes_ingestion.unpaywall, "enrich", fake_enrich)

    response = client.post("/collect", json={"topic": "anything"})

    assert response.status_code == 200
    body = response.json()
    assert 42 in body["ids"]


def test_collect_calls_download_anthology_export_before_search(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    monkeypatch.setattr(arxiv, "search_topic", lambda topic: [])
    monkeypatch.setattr(
        semantic_scholar, "search_topic", lambda topic, api_key=None: []
    )
    monkeypatch.setattr(
        acl_anthology,
        "download_anthology_export",
        lambda dest_path, force=False: calls.append("download"),
    )
    monkeypatch.setattr(
        acl_anthology,
        "search_topic",
        lambda bib_path, topic: calls.append("search") or [],
    )
    monkeypatch.setattr(
        routes_ingestion.unpaywall, "enrich", lambda session, email, dest_dir: []
    )

    response = client.post("/collect", json={"topic": "anything"})

    assert response.status_code == 200
    assert calls == ["download", "search"]


def test_upload_valid_pdf_creates_corpus_row(
    client: TestClient, test_session: Session
) -> None:
    response = client.post(
        "/upload",
        files={"files": ("paper.pdf", b"%PDF-1.4 fake content", "application/pdf")},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["created"]) == 1

    paper = test_session.query(Paper).filter_by(source="manual_upload").first()
    assert paper is not None


def test_upload_rejects_non_pdf(client: TestClient) -> None:
    response = client.post(
        "/upload",
        files={"files": ("image.png", b"not a pdf", "image/png")},
    )

    assert response.status_code == 415


def test_upload_rejects_oversized_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "0")

    response = client.post(
        "/upload",
        files={"files": ("paper.pdf", b"x" * 2000, "application/pdf")},
    )

    assert response.status_code == 413


def test_upload_rejects_oversized_file_multi_chunk(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-megabyte upload spanning several 1MB read chunks (_UPLOAD_CHUNK_SIZE)
    is still rejected with 413 — regression test for the chunked bounded-read path
    (code-review finding: a plain file.file.read() would buffer the whole upload
    before the size check ever ran)."""
    monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "1")  # 1MB cap

    oversized_content = b"x" * (5 * 1024 * 1024)  # 5MB, spans 5 read chunks

    response = client.post(
        "/upload",
        files={"files": ("paper.pdf", oversized_content, "application/pdf")},
    )

    assert response.status_code == 413


def test_upload_multiple_files_returns_one_result_each(client: TestClient) -> None:
    response = client.post(
        "/upload",
        files=[
            ("files", ("paper1.pdf", b"%PDF-1.4 one", "application/pdf")),
            ("files", ("paper2.pdf", b"%PDF-1.4 two", "application/pdf")),
        ],
    )

    assert response.status_code == 200
    assert len(response.json()["created"]) == 2
