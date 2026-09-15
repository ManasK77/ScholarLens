"""TestClient coverage for the live Phase 11 REST surface (`routes_backend`).

`routes_backend.py` could not be imported until Phase 10 landed (it needs
`langgraph` + `app.interactive.workflow`), so `app/main.py` did not register
its router for most of the project's history. Commit `26067fc` finally
registered it as a side effect of an unrelated-sounding commit ("Add PDF data
volume to Docker Compose and enhance pipeline handling") — no dedicated
Phase 11 TDD pass ever followed. The pre-existing
`tests/{explanation,ranking,trends}/test_api_contract.py` files predate
import-ability and say so in their own docstrings: they parse
`routes_backend.py` with `ast` instead of calling it, "until Phase 10 makes
that module importable, [when] these tests should be replaced by a
`TestClient` call." This file is that replacement for the auth and
request/response contract every one of these endpoints now serves over real
HTTP. It does not re-derive service-layer correctness — each phase's own
`service`/`storage` unit tests already own that.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SASession
from sqlalchemy.pool import StaticPool

from app.api import routes_backend
from app.api.security import API_KEY_HEADER_NAME, require_api_key
from app.clustering.schema import GapStatement, StatementCluster
from app.clustering.storage import replace_clusters
from app.explanation.llm import LLMError, LLMUnavailableError
from app.explanation.schema import GapReportRecord
from app.explanation.storage import replace_reports
from app.ingestion import corpus
from app.ranking.signals import GapSignals
from app.ranking.storage import replace_rankings
from app.trends.analysis import TrendSeries
from app.trends.storage import replace_trends

VALID_KEY = "test-backend-api-key"

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

PROTECTED_GET_ROUTES = [
    "/papers",
    "/graph",
    "/clusters",
    "/trends",
    "/gaps",
    "/reports",
]
PROTECTED_POST_ROUTES = [
    "/graph",
    "/index",
    "/clusters",
    "/trends",
    "/gaps",
    "/reports",
]


@pytest.fixture()
def app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("API_KEY", VALID_KEY)


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    corpus.Base.metadata.create_all(engine)
    with SASession(engine) as session:
        yield session


@pytest.fixture()
def unauthenticated_client(app_env: None) -> TestClient:
    """Real `require_api_key` wired in — used to prove auth is enforced."""
    application = FastAPI()
    application.include_router(routes_backend.router)
    return TestClient(application)


@pytest.fixture()
def client(app_env: None, db_session: SASession) -> TestClient:
    """Auth bypassed, real sqlite session — used to test handler behavior."""
    application = FastAPI()
    application.include_router(routes_backend.router)
    application.dependency_overrides[require_api_key] = lambda: None
    application.dependency_overrides[corpus.get_session] = lambda: db_session
    return TestClient(application)


# --------------------------------------------------------------------------
# Every route on this router requires the API key
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", PROTECTED_GET_ROUTES)
def test_get_routes_reject_requests_without_api_key(
    unauthenticated_client: TestClient, path: str
) -> None:
    response = unauthenticated_client.get(path)

    assert response.status_code == 401


@pytest.mark.parametrize("path", PROTECTED_POST_ROUTES)
def test_post_routes_reject_requests_without_api_key(
    unauthenticated_client: TestClient, path: str
) -> None:
    response = unauthenticated_client.post(path)

    assert response.status_code == 401


def test_pipeline_run_rejects_requests_without_api_key(
    unauthenticated_client: TestClient,
) -> None:
    response = unauthenticated_client.post("/pipeline/run", json={})

    assert response.status_code == 401


def test_query_rejects_requests_without_api_key(
    unauthenticated_client: TestClient,
) -> None:
    response = unauthenticated_client.post("/query", json={"question": "What gaps?"})

    assert response.status_code == 401


def test_protected_routes_are_rejected_when_server_key_is_unset(
    app_env: None, db_session: SASession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("API_KEY", raising=False)
    application = FastAPI()
    application.include_router(routes_backend.router)
    application.dependency_overrides[corpus.get_session] = lambda: db_session
    response = TestClient(application).get(
        "/papers", headers={API_KEY_HEADER_NAME: VALID_KEY}
    )

    assert response.status_code == 500


# --------------------------------------------------------------------------
# GET /papers
# --------------------------------------------------------------------------


def test_get_papers_lists_seeded_papers(
    client: TestClient, db_session: SASession
) -> None:
    db_session.add(
        corpus.Paper(
            title="A paper",
            authors=["A. Uthor"],
            year=2023,
            venue="ACL",
            source="arxiv",
            has_full_text=True,
            citation_count=5,
        )
    )
    db_session.commit()

    response = client.get("/papers")

    assert response.status_code == 200
    [paper] = response.json()
    assert paper["title"] == "A paper"
    assert paper["citation_count"] == 5


def test_get_papers_never_exposes_the_stored_pdf(
    client: TestClient, db_session: SASession
) -> None:
    """arXiv's full-text terms ask tools built on its PDFs to link back to arXiv
    rather than redistribute the file. ScholarLens downloads PDFs purely as
    pipeline input (parsing, extraction, embeddings), and `/papers` reports only
    *whether* a full text is held — never its path, and never the bytes. This
    guards that boundary: serving `pdf_path` would turn the corpus into a
    redistribution point."""
    db_session.add(
        corpus.Paper(
            title="A paper",
            source="arxiv",
            arxiv_id="2401.00001v1",
            pdf_path="/data/pdfs/2401.00001v1.pdf",
            has_full_text=True,
        )
    )
    db_session.commit()

    response = client.get("/papers")

    assert response.status_code == 200
    [paper] = response.json()
    assert paper["has_full_text"] is True
    assert "pdf_path" not in paper
    assert "/data/pdfs" not in response.text


def test_get_papers_filters_by_year(client: TestClient, db_session: SASession) -> None:
    db_session.add_all(
        [
            corpus.Paper(title="Old", year=2019, source="arxiv"),
            corpus.Paper(title="New", year=2024, source="arxiv"),
        ]
    )
    db_session.commit()

    response = client.get("/papers", params={"year": 2024})

    assert response.status_code == 200
    assert [paper["title"] for paper in response.json()] == ["New"]


# --------------------------------------------------------------------------
# GET/POST /clusters, /trends, /gaps, /reports
# --------------------------------------------------------------------------


def _seed_cluster(db_session: SASession) -> int:
    db_session.add(
        corpus.Paper(
            id=101, title="Paper 101", year=2023, source="arxiv", citation_count=4
        )
    )
    [cluster_id] = replace_clusters(
        db_session,
        [
            StatementCluster(
                label="multilingual",
                members=[
                    GapStatement.from_point(
                        {
                            "id": "point-101",
                            "paper_id": 101,
                            "text": "Evaluation covers English only.",
                            "year": 2023,
                            "source_type": "limitation",
                            "vector": [0.1, 0.2],
                        }
                    )
                ],
            )
        ],
    )
    db_session.commit()
    return cluster_id


def test_get_clusters_returns_seeded_cluster_with_members(
    client: TestClient, db_session: SASession
) -> None:
    _seed_cluster(db_session)

    response = client.get("/clusters")

    assert response.status_code == 200
    [cluster] = response.json()
    assert cluster["label"] == "multilingual"
    assert cluster["members"][0]["paper_id"] == 101


def test_post_clusters_rebuilds_and_commits(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routes_backend.clustering_service, "rebuild_clusters", lambda session: [1, 2]
    )

    response = client.post("/clusters")

    assert response.status_code == 200
    assert response.json() == {"clusters": [1, 2], "count": 2}


def test_get_trends_returns_seeded_series(
    client: TestClient, db_session: SASession
) -> None:
    cluster_id = _seed_cluster(db_session)
    replace_trends(db_session, [(cluster_id, TrendSeries({"2023": 1}, 0.0, 0))])
    db_session.commit()

    response = client.get("/trends")

    assert response.status_code == 200
    [trend] = response.json()
    assert trend["cluster_id"] == cluster_id
    assert trend["yearly_counts"] == {"2023": 1}


def test_post_trends_rebuilds_and_commits(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routes_backend.trend_service, "rebuild_trends", lambda session: [3]
    )

    response = client.post("/trends")

    assert response.status_code == 200
    assert response.json() == {"clusters": [3], "count": 1}


def test_get_gaps_returns_seeded_ranking(
    client: TestClient, db_session: SASession
) -> None:
    cluster_id = _seed_cluster(db_session)
    replace_rankings(
        db_session,
        [
            (
                cluster_id,
                12.5,
                GapSignals(
                    mention_frequency=4,
                    trend_slope=0.5,
                    solution_density=0.1,
                    citation_impact=2.0,
                    contradiction_flag=0.0,
                ),
            )
        ],
    )
    db_session.commit()

    response = client.get("/gaps")

    assert response.status_code == 200
    [gap] = response.json()
    assert gap["cluster_id"] == cluster_id
    assert gap["score"] == 12.5
    assert gap["signals"]["mention_frequency"] == 4


def test_post_gaps_rebuilds_and_commits(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routes_backend.ranking_service, "rebuild_rankings", lambda session: [7]
    )

    response = client.post("/gaps")

    assert response.status_code == 200
    assert response.json() == {"clusters": [7], "count": 1}


def test_get_reports_returns_seeded_report(
    client: TestClient, db_session: SASession
) -> None:
    cluster_id = _seed_cluster(db_session)
    replace_reports(
        db_session,
        [
            GapReportRecord(
                cluster_id=cluster_id,
                gap="Multilingual evaluation is underexplored.",
                evidence=[{"id": "e1"}],
                confidence=0.8,
                suggested_directions=["Build a multilingual benchmark."],
                provider="groq",
                model="groq-test",
            )
        ],
    )
    db_session.commit()

    response = client.get("/reports")

    assert response.status_code == 200
    [report] = response.json()
    assert report["cluster_id"] == cluster_id
    assert report["confidence"] == 0.8
    assert report["provider"] == "groq"


def test_post_reports_generates_and_commits(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routes_backend.report_service, "generate_top_reports", lambda session: [9]
    )

    response = client.post("/reports")

    assert response.status_code == 200
    assert response.json() == {"clusters": [9], "count": 1}


def test_post_reports_returns_502_and_never_leaks_the_exception_on_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(session):
        raise RuntimeError("secret internal detail: db password=hunter2")

    monkeypatch.setattr(routes_backend.report_service, "generate_top_reports", _boom)

    response = client.post("/reports")

    assert response.status_code == 502
    assert "hunter2" not in response.text
    assert response.json() == {"detail": "Report generation failed"}


# --------------------------------------------------------------------------
# /graph, /index
# --------------------------------------------------------------------------


def test_post_graph_extracts_and_commits(
    client: TestClient, db_session: SASession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routes_backend, "graph_session", lambda *a, **k: _NullContext(object())
    )
    monkeypatch.setattr(
        routes_backend.graph_service, "extract_all", lambda session, neo4j: [1, 2, 3]
    )

    response = client.post("/graph")

    assert response.status_code == 200
    assert response.json() == {"processed": [1, 2, 3], "count": 3}


def test_get_graph_returns_snapshot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routes_backend, "graph_session", lambda *a, **k: _NullContext(object())
    )
    monkeypatch.setattr(
        routes_backend,
        "graph_snapshot",
        lambda neo4j, limit: {"nodes": [], "edges": []},
    )

    response = client.get("/graph")

    assert response.status_code == 200
    assert response.json() == {"nodes": [], "edges": []}


def test_post_index_indexes_and_returns_count(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routes_backend, "graph_session", lambda *a, **k: _NullContext(object())
    )
    monkeypatch.setattr(routes_backend, "index_all", lambda session, neo4j: 42)

    response = client.post("/index")

    assert response.status_code == 200
    assert response.json() == {"indexed": 42}


class _NullContext:
    def __init__(self, value):
        self._value = value

    def __enter__(self):
        return self._value

    def __exit__(self, *exc_info):
        return False


# --------------------------------------------------------------------------
# /pipeline/run
# --------------------------------------------------------------------------


def test_pipeline_run_calls_every_stage_and_commits(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routes_backend,
        "get_settings",
        lambda: SimpleNamespace(
            grobid_url="http://grobid:8070",
            unpaywall_email="you@example.com",
            pdf_storage_path="/data/pdfs",
        ),
    )
    monkeypatch.setattr(
        routes_backend,
        "parse_all_pending",
        lambda *a, **k: SimpleNamespace(processed=1, missing_pdfs=[]),
    )
    monkeypatch.setattr(routes_backend, "extract_all_parsed", lambda session: 2)
    monkeypatch.setattr(
        routes_backend, "graph_session", lambda *a, **k: _NullContext(object())
    )
    monkeypatch.setattr(
        routes_backend.graph_service, "extract_all", lambda session, neo4j: [1]
    )
    monkeypatch.setattr(routes_backend, "EvidenceIndex", lambda settings: object())
    monkeypatch.setattr(
        routes_backend, "index_all", lambda session, neo4j, index=None: 3
    )
    monkeypatch.setattr(
        routes_backend.clustering_service,
        "rebuild_clusters",
        lambda session, settings=None: [1],
    )
    monkeypatch.setattr(
        routes_backend.trend_service, "rebuild_trends", lambda session: [1]
    )
    monkeypatch.setattr(
        routes_backend.ranking_service,
        "rebuild_rankings",
        lambda session, settings=None: [1],
    )
    monkeypatch.setattr(
        routes_backend.report_service,
        "generate_top_reports",
        lambda session, settings=None: [1],
    )

    response = client.post("/pipeline/run", json={"generate_reports": True})

    assert response.status_code == 200
    body = response.json()
    assert body["parsed"] == 1
    assert body["extracted"] == 2
    assert body["indexed"] == 3
    assert body["reports"] == [1]


def test_pipeline_run_skips_report_generation_when_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routes_backend,
        "get_settings",
        lambda: SimpleNamespace(
            grobid_url="http://grobid:8070",
            unpaywall_email="you@example.com",
            pdf_storage_path="/data/pdfs",
        ),
    )
    monkeypatch.setattr(
        routes_backend,
        "parse_all_pending",
        lambda *a, **k: SimpleNamespace(processed=0, missing_pdfs=[]),
    )
    monkeypatch.setattr(routes_backend, "extract_all_parsed", lambda session: 0)
    monkeypatch.setattr(
        routes_backend, "graph_session", lambda *a, **k: _NullContext(object())
    )
    monkeypatch.setattr(
        routes_backend.graph_service, "extract_all", lambda session, neo4j: []
    )
    monkeypatch.setattr(routes_backend, "EvidenceIndex", lambda settings: object())
    monkeypatch.setattr(
        routes_backend, "index_all", lambda session, neo4j, index=None: 0
    )
    monkeypatch.setattr(
        routes_backend.clustering_service,
        "rebuild_clusters",
        lambda session, settings=None: [],
    )
    monkeypatch.setattr(
        routes_backend.trend_service, "rebuild_trends", lambda session: []
    )
    monkeypatch.setattr(
        routes_backend.ranking_service,
        "rebuild_rankings",
        lambda session, settings=None: [],
    )
    called = []
    monkeypatch.setattr(
        routes_backend.report_service,
        "generate_top_reports",
        lambda *a, **k: called.append(True),
    )

    response = client.post("/pipeline/run", json={"generate_reports": False})

    assert response.status_code == 200
    assert response.json()["reports"] == []
    assert called == []


# --------------------------------------------------------------------------
# OpenAPI docs — Phase 11's "Adds OpenAPI docs" criterion
# --------------------------------------------------------------------------


def test_full_app_serves_openapi_docs_for_every_required_endpoint() -> None:
    from app.main import app as full_app

    schema = full_app.openapi()
    required = {
        "/collect",
        "/upload",
        "/papers",
        "/graph",
        "/clusters",
        "/trends",
        "/gaps",
        "/reports",
        "/query",
    }

    assert required <= set(schema["paths"])
    for path in required:
        for operation in schema["paths"][path].values():
            assert operation.get("security"), f"{path} is not marked as authenticated"


def _stub_evidence_stages(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    """Stub every pre-LLM stage of `/pipeline/run`, recording the ranking step."""
    monkeypatch.setattr(
        routes_backend,
        "get_settings",
        lambda: SimpleNamespace(
            grobid_url="http://grobid:8070",
            unpaywall_email="you@example.com",
            pdf_storage_path="/data/pdfs",
        ),
    )
    monkeypatch.setattr(
        routes_backend,
        "parse_all_pending",
        lambda *a, **k: SimpleNamespace(processed=[1], missing_pdfs=[]),
    )
    monkeypatch.setattr(routes_backend, "extract_all_parsed", lambda session: [1])
    monkeypatch.setattr(
        routes_backend, "graph_session", lambda *a, **k: _NullContext(object())
    )
    monkeypatch.setattr(
        routes_backend.graph_service, "extract_all", lambda session, neo4j: [1]
    )
    monkeypatch.setattr(routes_backend, "EvidenceIndex", lambda settings: object())
    monkeypatch.setattr(
        routes_backend, "index_all", lambda session, neo4j, index=None: 4
    )
    monkeypatch.setattr(
        routes_backend.clustering_service,
        "rebuild_clusters",
        lambda session, settings=None: [1],
    )
    monkeypatch.setattr(
        routes_backend.trend_service, "rebuild_trends", lambda session: [1]
    )

    def _rankings(session, settings=None):
        events.append("rankings")
        return [1]

    monkeypatch.setattr(routes_backend.ranking_service, "rebuild_rankings", _rankings)


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (LLMUnavailableError("Groq is unavailable. Check network access."), 503),
        (LLMError("GROQ_API_KEY is required to call the Groq API"), 502),
    ],
)
def test_pipeline_run_keeps_the_evidence_base_when_report_generation_fails(
    client: TestClient,
    db_session: SASession,
    monkeypatch: pytest.MonkeyPatch,
    error: LLMError,
    status_code: int,
) -> None:
    """README §12: with no Groq key, every non-LLM stage "still works".

    The handler committed once, at the very end, so an LLM failure in the last
    stage rolled back parsing, extraction, clusters, trends and rankings, while
    the Neo4j graph and Qdrant vectors written mid-run survived.
    """
    events: list[str] = []
    _stub_evidence_stages(monkeypatch, events)
    real_commit = db_session.commit

    def _recording_commit() -> None:
        events.append("commit")
        real_commit()

    monkeypatch.setattr(db_session, "commit", _recording_commit)

    def _reports(session, settings=None):
        events.append("reports")
        raise error

    monkeypatch.setattr(routes_backend.report_service, "generate_top_reports", _reports)

    response = client.post("/pipeline/run", json={"generate_reports": True})

    assert response.status_code == status_code
    assert str(error) in response.json()["detail"]
    assert events.index("rankings") < events.index("commit") < events.index("reports")


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (LLMUnavailableError("Groq rate limit reached. Retry later."), 503),
        (LLMError("GROQ_API_KEY is required to call the Groq API"), 502),
    ],
)
def test_post_reports_names_the_llm_failure_instead_of_a_generic_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    error: LLMError,
    status_code: int,
) -> None:
    """README §12: report generation returns an explicit Groq error."""

    def _fail(session):
        raise error

    monkeypatch.setattr(routes_backend.report_service, "generate_top_reports", _fail)

    response = client.post("/reports")

    assert response.status_code == status_code
    assert response.json() == {"detail": str(error)}
