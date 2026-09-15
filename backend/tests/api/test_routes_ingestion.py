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
    "GROBID_URL": "http://grobid:8070",
    # /collect and /upload sit behind the Phase 11 API key.
    "API_KEY": "test-api-key-ingestion",
}

AUTH_HEADERS = {"X-API-Key": REQUIRED_ENV["API_KEY"]}


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
    yield TestClient(app, headers=AUTH_HEADERS)
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

    monkeypatch.setattr(
        arxiv, "search_topic", lambda topic, **kwargs: [arxiv_candidate]
    )
    monkeypatch.setattr(arxiv, "fetch_pdf", lambda candidate, dest_dir, **kw: None)
    monkeypatch.setattr(
        semantic_scholar,
        "search_topic",
        lambda topic, api_key=None, **kwargs: [s2_candidate],
    )
    monkeypatch.setattr(
        acl_anthology, "download_anthology_export", lambda dest_path, force=False: None
    )
    monkeypatch.setattr(
        acl_anthology, "search_topic", lambda bib_path, topic: [acl_candidate]
    )
    monkeypatch.setattr(
        acl_anthology, "fetch_pdf", lambda candidate, dest_dir, **kw: None
    )

    def fake_enrich(session, email, dest_dir, **kw):
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
    monkeypatch.setattr(arxiv, "search_topic", lambda topic, **kwargs: [])
    monkeypatch.setattr(
        semantic_scholar, "search_topic", lambda topic, api_key=None, **kwargs: []
    )
    monkeypatch.setattr(
        acl_anthology, "download_anthology_export", lambda dest_path, force=False: None
    )
    monkeypatch.setattr(acl_anthology, "search_topic", lambda bib_path, topic: [])

    def fake_enrich(session, email, dest_dir, **kw):
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

    monkeypatch.setattr(arxiv, "search_topic", lambda topic, **kwargs: [])
    monkeypatch.setattr(
        semantic_scholar, "search_topic", lambda topic, api_key=None, **kwargs: []
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
        routes_ingestion.unpaywall, "enrich", lambda session, email, dest_dir, **kw: []
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


def test_paper_dict_carries_the_citation_count_onto_the_corpus_row():
    """The one place a source's raw dict becomes Paper columns.

    Semantic Scholar is the only source that reports a count; the others leave
    it `None`, and `dedup.merge` (fill-if-None) lets a later S2 match supply it
    without ever overwriting a known value.
    """
    mapped = routes_ingestion._to_paper_dict(
        {"title": "A paper", "citation_count": 412}, "semantic_scholar", None
    )

    assert mapped["citation_count"] == 412
    assert (
        routes_ingestion._to_paper_dict({"title": "A paper"}, "arxiv", None)[
            "citation_count"
        ]
        is None
    )


def test_collect_survives_one_failing_discovery_source(
    client: TestClient, test_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flaky third-party source must not discard the papers others found.

    Semantic Scholar answers unauthenticated callers with 429/500 routinely, and
    before this an exception there aborted the request — rolling back every
    arXiv row already written while their PDFs stayed on disk.
    """
    import httpx

    monkeypatch.setattr(
        arxiv,
        "search_topic",
        lambda topic, **kwargs: [
            {"title": "Survivor", "year": 2024, "arxiv_id": "2401.00001", "doi": None}
        ],
    )
    monkeypatch.setattr(arxiv, "fetch_pdf", lambda candidate, dest_dir, **kw: None)

    def _rate_limited(topic, api_key=None, **kwargs):
        raise httpx.HTTPStatusError(
            "429",
            request=httpx.Request("GET", "https://api.semanticscholar.org"),
            response=httpx.Response(429),
        )

    monkeypatch.setattr(semantic_scholar, "search_topic", _rate_limited)
    monkeypatch.setattr(
        acl_anthology, "download_anthology_export", lambda dest_path, force=False: None
    )
    monkeypatch.setattr(acl_anthology, "search_topic", lambda bib_path, topic: [])
    monkeypatch.setattr(
        routes_ingestion.unpaywall, "enrich", lambda session, email, dest_dir, **kw: []
    )

    response = client.post("/collect", json={"topic": "prompt injection"})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["sources"]["arxiv"] == "ok"
    assert body["sources"]["semantic_scholar"].startswith("failed")
    assert body["sources"]["acl_anthology"] == "ok"
    # The surviving paper is committed, not rolled back with the failing source.
    assert test_session.query(Paper).count() == 1


def test_collect_survives_a_failing_acl_anthology(
    client: TestClient, test_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third leg of the isolation matrix: a broken or absent Anthology
    export costs ACL's papers, not arXiv's or Semantic Scholar's."""

    def _broken_export(dest_path, force=False):
        raise OSError("anthology export unavailable")

    monkeypatch.setattr(
        arxiv, "search_topic", lambda topic, **kwargs: _arxiv_candidates(1)
    )
    monkeypatch.setattr(arxiv, "fetch_pdf", lambda candidate, dest_dir, **kw: None)
    monkeypatch.setattr(
        semantic_scholar,
        "search_topic",
        lambda topic, api_key=None, **kwargs: _s2_candidates(1),
    )
    monkeypatch.setattr(acl_anthology, "download_anthology_export", _broken_export)
    monkeypatch.setattr(
        routes_ingestion.unpaywall, "enrich", lambda session, email, dest_dir, **kw: []
    )

    response = client.post("/collect", json={"topic": "anything"})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["sources"]["arxiv"] == "ok"
    assert body["sources"]["semantic_scholar"] == "ok"
    assert body["sources"]["acl_anthology"].startswith("failed")
    assert test_session.query(Paper).count() == 2


def test_collect_reports_a_failing_enrichment_step_without_losing_papers(
    client: TestClient, test_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        arxiv,
        "search_topic",
        lambda topic, **kwargs: [
            {"title": "Kept", "year": 2024, "arxiv_id": "2401.00002", "doi": None}
        ],
    )
    monkeypatch.setattr(arxiv, "fetch_pdf", lambda candidate, dest_dir, **kw: None)
    monkeypatch.setattr(
        semantic_scholar, "search_topic", lambda topic, api_key=None, **kwargs: []
    )
    monkeypatch.setattr(
        acl_anthology, "download_anthology_export", lambda dest_path, force=False: None
    )
    monkeypatch.setattr(acl_anthology, "search_topic", lambda bib_path, topic: [])

    def _boom(session, email, dest_dir):
        raise RuntimeError("unpaywall down")

    monkeypatch.setattr(routes_ingestion.unpaywall, "enrich", _boom)

    response = client.post("/collect", json={"topic": "anything"})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["sources"]["unpaywall"].startswith("failed")
    assert test_session.query(Paper).count() == 1


def test_collect_fails_loudly_when_every_discovery_source_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three sources down is an outage, not an empty result set."""

    def _down(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(arxiv, "search_topic", _down)
    monkeypatch.setattr(semantic_scholar, "search_topic", _down)
    monkeypatch.setattr(acl_anthology, "download_anthology_export", _down)
    monkeypatch.setattr(acl_anthology, "search_topic", _down)
    monkeypatch.setattr(
        routes_ingestion.unpaywall, "enrich", lambda session, email, dest_dir, **kw: []
    )

    response = client.post("/collect", json={"topic": "anything"})

    assert response.status_code == 502


def test_collect_reports_every_source_ok_on_a_clean_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(arxiv, "search_topic", lambda topic, **kwargs: [])
    monkeypatch.setattr(
        semantic_scholar, "search_topic", lambda topic, api_key=None, **kwargs: []
    )
    monkeypatch.setattr(
        acl_anthology, "download_anthology_export", lambda dest_path, force=False: None
    )
    monkeypatch.setattr(acl_anthology, "search_topic", lambda bib_path, topic: [])
    monkeypatch.setattr(
        routes_ingestion.unpaywall, "enrich", lambda session, email, dest_dir, **kw: []
    )

    body = client.post("/collect", json={"topic": "anything"}).json()

    assert body["sources"] == {
        "arxiv": "ok",
        "semantic_scholar": "ok",
        "acl_anthology": "ok",
        "unpaywall": "ok",
    }


def test_collect_passes_the_configured_request_limits_to_each_source(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Source modules never read Settings; /collect hands them their limits."""
    monkeypatch.setenv("ARXIV_MAX_RESULTS", "7")
    monkeypatch.setenv("ARXIV_NUM_RETRIES", "0")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_MAX_RESULTS", "9")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_MAX_RETRIES", "2")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_BACKOFF_BASE_SECONDS", "0.5")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_BACKOFF_MAX_SECONDS", "4")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_MIN_INTERVAL_SECONDS", "1.25")
    monkeypatch.setenv("ARXIV_MIN_INTERVAL_SECONDS", "4")
    monkeypatch.setenv("ARXIV_BACKOFF_BASE_SECONDS", "7")
    monkeypatch.setenv("ARXIV_BACKOFF_MAX_SECONDS", "45")
    monkeypatch.setenv("ARXIV_CONTACT", "ops@example.org")
    seen: dict[str, dict] = {}

    def _arxiv(topic, **kwargs):
        seen["arxiv"] = kwargs
        return []

    def _semantic_scholar(topic, api_key=None, **kwargs):
        seen["semantic_scholar"] = kwargs
        return []

    monkeypatch.setattr(arxiv, "search_topic", _arxiv)
    monkeypatch.setattr(semantic_scholar, "search_topic", _semantic_scholar)
    monkeypatch.setattr(
        acl_anthology, "download_anthology_export", lambda dest_path, force=False: None
    )
    monkeypatch.setattr(acl_anthology, "search_topic", lambda bib_path, topic: [])
    monkeypatch.setattr(
        routes_ingestion.unpaywall, "enrich", lambda session, email, dest_dir, **kw: []
    )

    response = client.post("/collect", json={"topic": "anything"})

    assert response.status_code == 200
    assert seen["arxiv"] == {
        "max_results": 7,
        "num_retries": 0,
        "min_interval_seconds": 4.0,
        "backoff_base_seconds": 7.0,
        "backoff_max_seconds": 45.0,
        "contact": "ops@example.org",
    }
    assert seen["semantic_scholar"] == {
        "limit": 9,
        "max_retries": 2,
        "min_interval_seconds": 1.25,
        "backoff_base_seconds": 0.5,
        "backoff_max_seconds": 4.0,
    }


def test_collect_passes_the_configured_politeness_to_arxiv_pdf_downloads(
    client: TestClient, test_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-PDF-01: a PDF fetch is an arXiv request, so it carries the same
    contact, spacing and retry budget the metadata request does."""
    monkeypatch.setenv("ARXIV_NUM_RETRIES", "1")
    monkeypatch.setenv("ARXIV_MIN_INTERVAL_SECONDS", "4")
    monkeypatch.setenv("ARXIV_BACKOFF_BASE_SECONDS", "7")
    monkeypatch.setenv("ARXIV_BACKOFF_MAX_SECONDS", "45")
    monkeypatch.setenv("ARXIV_CONTACT", "ops@example.org")
    seen: list[dict] = []

    monkeypatch.setattr(
        arxiv, "search_topic", lambda topic, **kwargs: _arxiv_candidates(1)
    )
    monkeypatch.setattr(
        arxiv,
        "download_pdf",
        lambda url, dest_path, **kwargs: seen.append(kwargs) or False,
    )
    monkeypatch.setattr(
        semantic_scholar, "search_topic", lambda topic, api_key=None, **kwargs: []
    )
    monkeypatch.setattr(
        acl_anthology, "download_anthology_export", lambda dest_path, force=False: None
    )
    monkeypatch.setattr(acl_anthology, "search_topic", lambda bib_path, topic: [])
    monkeypatch.setattr(
        routes_ingestion.unpaywall, "enrich", lambda session, email, dest_dir, **kw: []
    )

    response = client.post("/collect", json={"topic": "anything"})

    assert response.status_code == 200
    assert seen == [
        {
            "contact": "ops@example.org",
            "num_retries": 1,
            "min_interval_seconds": 4.0,
            "backoff_base_seconds": 7.0,
            "backoff_max_seconds": 45.0,
        }
    ]


def test_collect_gives_unpaywall_a_polite_downloader_for_arxiv_copies(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OA copy Unpaywall resolves to arXiv is paced like any arXiv request."""
    monkeypatch.setenv("ARXIV_CONTACT", "ops@example.org")
    monkeypatch.setenv("ARXIV_MIN_INTERVAL_SECONDS", "4")
    seen: list[dict] = []

    def _enrich(session, email, dest_dir, download_arxiv_pdf=None):
        download_arxiv_pdf("https://arxiv.org/pdf/2401.00001v1", "/tmp/42.pdf")
        return []

    monkeypatch.setattr(arxiv, "search_topic", lambda topic, **kwargs: [])
    monkeypatch.setattr(
        arxiv,
        "download_pdf",
        lambda url, dest_path, **kwargs: seen.append(kwargs) or False,
    )
    monkeypatch.setattr(
        semantic_scholar, "search_topic", lambda topic, api_key=None, **kwargs: []
    )
    monkeypatch.setattr(
        acl_anthology, "download_anthology_export", lambda dest_path, force=False: None
    )
    monkeypatch.setattr(acl_anthology, "search_topic", lambda bib_path, topic: [])
    monkeypatch.setattr(routes_ingestion.unpaywall, "enrich", _enrich)

    response = client.post("/collect", json={"topic": "anything"})

    assert response.status_code == 200
    assert seen and seen[0]["contact"] == "ops@example.org"
    assert seen[0]["min_interval_seconds"] == 4.0


def test_collect_keeps_a_paper_whose_pdf_download_fails(
    client: TestClient, test_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One throttled or missing PDF costs that paper its full text, nothing more."""
    monkeypatch.setattr(
        arxiv, "search_topic", lambda topic, **kwargs: _arxiv_candidates(2)
    )
    monkeypatch.setattr(arxiv, "download_pdf", lambda url, dest_path, **kwargs: False)
    monkeypatch.setattr(
        semantic_scholar, "search_topic", lambda topic, api_key=None, **kwargs: []
    )
    monkeypatch.setattr(
        acl_anthology, "download_anthology_export", lambda dest_path, force=False: None
    )
    monkeypatch.setattr(acl_anthology, "search_topic", lambda bib_path, topic: [])
    monkeypatch.setattr(
        routes_ingestion.unpaywall, "enrich", lambda session, email, dest_dir, **kw: []
    )

    response = client.post("/collect", json={"topic": "anything"})

    assert response.status_code == 200
    assert response.json()["sources"]["arxiv"] == "ok"
    assert test_session.query(Paper).count() == 2
    assert [paper.has_full_text for paper in test_session.query(Paper).all()] == [
        False,
        False,
    ]


def test_collect_makes_one_arxiv_request_when_throttled_without_retry_after(
    client: TestClient, test_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-429-03 end to end: arXiv's live `429 Rate exceeded.` carries no
    Retry-After, so the whole run makes exactly one arXiv request — no retries
    and no PDF fetches behind them — and the other sources still deliver."""
    from tests.ingestion.fake_clock import FakeClock
    from tests.ingestion.sources.test_arxiv import FakeAdapter

    adapter = FakeAdapter([(429, b"Rate exceeded.", {})] * 6)
    fake = FakeClock()
    real_search_topic = arxiv.search_topic

    monkeypatch.setattr(
        arxiv,
        "search_topic",
        lambda topic, **kwargs: real_search_topic(
            topic, adapter=adapter, limiter=fake.limiter(), **kwargs
        ),
    )
    monkeypatch.setattr(
        semantic_scholar,
        "search_topic",
        lambda topic, api_key=None, **kwargs: _s2_candidates(2),
    )
    monkeypatch.setattr(
        acl_anthology, "download_anthology_export", lambda dest_path, force=False: None
    )
    monkeypatch.setattr(acl_anthology, "search_topic", lambda bib_path, topic: [])
    monkeypatch.setattr(
        routes_ingestion.unpaywall, "enrich", lambda session, email, dest_dir, **kw: []
    )

    response = client.post("/collect", json={"topic": "prompt injection"})

    assert response.status_code == 200
    assert len(adapter.sent) == 1
    body = response.json()
    assert body["sources"]["arxiv"].startswith("failed")
    assert "429" in body["sources"]["arxiv"]
    assert body["sources"]["semantic_scholar"] == "ok"
    assert body["sources"]["acl_anthology"] == "ok"
    assert test_session.query(Paper).count() == 2


def test_collect_reports_an_exhausted_arxiv_source_and_keeps_the_others(
    client: TestClient, test_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """arXiv still throttled after its bounded retries is one failed source,
    not a failed run."""
    import arxiv as arxiv_library

    def _throttled(topic, **kwargs):
        raise arxiv_library.HTTPError("https://export.arxiv.org/api/query", 2, 429)

    monkeypatch.setattr(arxiv, "search_topic", _throttled)
    monkeypatch.setattr(
        semantic_scholar,
        "search_topic",
        lambda topic, api_key=None, **kwargs: [
            {"title": "From S2", "year": 2024, "doi": "10.1/s2"}
        ],
    )
    monkeypatch.setattr(
        acl_anthology, "download_anthology_export", lambda dest_path, force=False: None
    )
    monkeypatch.setattr(acl_anthology, "search_topic", lambda bib_path, topic: [])
    monkeypatch.setattr(
        routes_ingestion.unpaywall, "enrich", lambda session, email, dest_dir, **kw: []
    )

    response = client.post("/collect", json={"topic": "prompt injection"})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["sources"]["arxiv"].startswith("failed")
    assert "429" in body["sources"]["arxiv"]
    assert body["sources"]["semantic_scholar"] == "ok"
    assert body["sources"]["acl_anthology"] == "ok"
    assert body["sources"]["unpaywall"] == "ok"
    assert test_session.query(Paper).count() == 1


# --- Per-source caps: each automatic source adds at most N papers per run ---


def _arxiv_candidates(n: int, prefix: str = "2401") -> list[dict]:
    return [
        {
            "title": f"arXiv paper {i}",
            "year": 2024,
            "doi": None,
            "arxiv_id": f"{prefix}.{i:05d}",
            "pdf_url": f"https://arxiv.org/pdf/{prefix}.{i:05d}",
        }
        for i in range(n)
    ]


def _s2_candidates(n: int) -> list[dict]:
    return [
        {"title": f"S2 paper {i}", "year": 2023, "doi": f"10.1/s2-{i}"}
        for i in range(n)
    ]


def _acl_candidates(n: int) -> list[dict]:
    return [
        {
            "title": f"ACL paper {i}",
            "year": 2022,
            "doi": None,
            "acl_id": f"2022.acl-long.{i}",
            "pdf_url": f"https://aclanthology.org/2022.acl-long.{i}.pdf",
        }
        for i in range(n)
    ]


def _stub_sources(
    monkeypatch: pytest.MonkeyPatch,
    *,
    arxiv_results: list[dict],
    s2_results: list[dict],
    acl_results: list[dict],
    pdf_fetches: list[str] | None = None,
) -> None:
    def _fetch(candidate, dest_dir, **kw):
        """Record the fetch and fail it, so every row stays metadata-only."""
        if pdf_fetches is not None:
            pdf_fetches.append(candidate.get("arxiv_id") or candidate.get("acl_id"))

    monkeypatch.setattr(arxiv, "search_topic", lambda topic, **kwargs: arxiv_results)
    monkeypatch.setattr(arxiv, "fetch_pdf", _fetch)
    monkeypatch.setattr(
        semantic_scholar,
        "search_topic",
        lambda topic, api_key=None, **kwargs: s2_results,
    )
    monkeypatch.setattr(
        acl_anthology, "download_anthology_export", lambda dest_path, force=False: None
    )
    monkeypatch.setattr(
        acl_anthology, "search_topic", lambda bib_path, topic: acl_results
    )
    monkeypatch.setattr(acl_anthology, "fetch_pdf", _fetch)
    monkeypatch.setattr(
        routes_ingestion.unpaywall, "enrich", lambda session, email, dest_dir, **kw: []
    )


def test_collect_caps_every_automatic_source_at_ten_papers(
    client: TestClient, test_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oversupplying sources never pushes one past its 10-paper default cap —
    and no PDF is downloaded for a candidate beyond the cap."""
    fetched: list[str] = []
    _stub_sources(
        monkeypatch,
        arxiv_results=_arxiv_candidates(15),
        s2_results=_s2_candidates(15),
        acl_results=_acl_candidates(40),
        pdf_fetches=fetched,
    )

    body = client.post("/collect", json={"topic": "anything"}).json()

    assert body["count"] == 30
    for source in ("arxiv", "semantic_scholar", "acl_anthology"):
        assert test_session.query(Paper).filter_by(source=source).count() == 10
    assert len(fetched) == 20  # 10 arXiv + 10 ACL, none past either cap


def test_collect_honours_a_lower_configured_cap_per_source(
    client: TestClient, test_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARXIV_MAX_RESULTS", "2")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_MAX_RESULTS", "3")
    monkeypatch.setenv("ACL_ANTHOLOGY_MAX_RESULTS", "4")
    _stub_sources(
        monkeypatch,
        arxiv_results=_arxiv_candidates(10),
        s2_results=_s2_candidates(10),
        acl_results=_acl_candidates(10),
    )

    body = client.post("/collect", json={"topic": "anything"}).json()

    assert body["count"] == 9
    assert test_session.query(Paper).filter_by(source="arxiv").count() == 2
    assert test_session.query(Paper).filter_by(source="semantic_scholar").count() == 3
    assert test_session.query(Paper).filter_by(source="acl_anthology").count() == 4


def test_collect_keeps_whatever_a_source_has_when_fewer_than_the_cap(
    client: TestClient, test_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thin topic is a small result, not a failure."""
    _stub_sources(
        monkeypatch,
        arxiv_results=_arxiv_candidates(3),
        s2_results=[],
        acl_results=_acl_candidates(1),
    )

    response = client.post("/collect", json={"topic": "anything"})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 4
    assert body["sources"]["arxiv"] == "ok"
    assert body["sources"]["semantic_scholar"] == "ok"
    assert body["sources"]["acl_anthology"] == "ok"


def test_collect_duplicates_within_a_source_do_not_use_up_its_cap(
    client: TestClient, test_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two ACL records that dedup to one row count once, so the cap still
    fills with 10 distinct papers when the source has them."""
    acl = _acl_candidates(11)
    # Same title+year as paper 0 under a different Anthology ID: the fallback
    # dedup rule merges it into paper 0's row.
    acl.insert(1, {**acl[0], "acl_id": "2022.findings-acl.99"})
    _stub_sources(monkeypatch, arxiv_results=[], s2_results=[], acl_results=acl)

    body = client.post("/collect", json={"topic": "anything"}).json()

    assert body["count"] == 10
    assert test_session.query(Paper).filter_by(source="acl_anthology").count() == 10


def test_collect_cross_source_duplicates_still_merge_under_the_caps(
    client: TestClient, test_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    arxiv_results = _arxiv_candidates(10)
    arxiv_results[0]["doi"] = "10.1/s2-0"  # also Semantic Scholar's first hit
    _stub_sources(
        monkeypatch,
        arxiv_results=arxiv_results,
        s2_results=_s2_candidates(12),
        acl_results=[],
    )

    body = client.post("/collect", json={"topic": "anything"}).json()

    # 10 arXiv rows; S2 keeps 10 papers, one of which merges into an arXiv row.
    assert body["count"] == 19
    assert test_session.query(Paper).count() == 19
    assert test_session.query(Paper).filter_by(doi="10.1/s2-0").count() == 1


def test_upload_is_not_subject_to_the_discovery_caps(client: TestClient) -> None:
    files = [
        ("files", (f"paper{i}.pdf", b"%PDF-1.4 " + bytes([65 + i]), "application/pdf"))
        for i in range(12)
    ]

    response = client.post("/upload", files=files)

    assert response.status_code == 200
    assert len(response.json()["created"]) == 12
