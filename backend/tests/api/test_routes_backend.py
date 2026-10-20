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
from app.explanation.llm import (
    LLMAuthError,
    LLMConfigurationError,
    LLMError,
    LLMRateLimitError,
    LLMUnavailableError,
)
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


def test_pipeline_rerun_skips_unchanged_derived_work(client, monkeypatch):
    events = []
    _stub_evidence_stages(monkeypatch, events)
    assert (
        client.post("/pipeline/run", json={"generate_reports": False}).status_code
        == 200
    )
    assert (
        client.post("/pipeline/run", json={"generate_reports": False}).status_code
        == 200
    )
    assert events == ["rankings"]


def test_all_writers_reject_lock_contention(client, db_session):
    from app.core.pipeline import writer_lock

    with writer_lock(db_session):
        for path in [
            "/pipeline/run",
            "/graph",
            "/index",
            "/clusters",
            "/trends",
            "/gaps",
            "/reports",
        ]:
            assert client.post(path, json={}).status_code == 409


def test_failed_clustering_preserves_old_reports_and_no_completion_marker(
    client, db_session, monkeypatch
):
    from sqlalchemy import select

    from app.core.pipeline import DerivedState, PipelineRun
    from app.explanation.storage import list_reports

    cluster_id = _seed_cluster(db_session)
    replace_reports(
        db_session,
        [
            GapReportRecord(
                cluster_id=cluster_id,
                gap="Stored gap",
                evidence=[{"id": "e1"}],
                confidence=0.8,
                suggested_directions=["Investigate"],
                provider="groq",
                model="test",
            )
        ],
    )
    db_session.commit()
    _stub_evidence_stages(monkeypatch, [])

    def fail(session, settings=None):
        replace_clusters(session, [])
        raise RuntimeError("injected failure")

    monkeypatch.setattr(routes_backend.clustering_service, "rebuild_clusters", fail)
    with pytest.raises(RuntimeError):
        client.post("/pipeline/run", json={"generate_reports": False})
    assert list_reports(db_session)
    assert db_session.get(DerivedState, "s6-s8") is None
    assert db_session.scalar(select(PipelineRun)).status == "failed"


PROTECTED_GET_ROUTES = [
    "/papers",
    "/graph",
    "/clusters",
    "/trends",
    "/gaps",
    "/reports",
]
PROTECTED_POST_ROUTES = [
    "/parse",
    "/extract",
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
                # The six keys `schema.evidence_from_point` always stores;
                # `/reports` is typed against that shape.
                evidence=[
                    {
                        "id": "e1",
                        "paper_id": 3,
                        "title": "A paper",
                        "text": "Evaluation is English-only.",
                        "year": 2024,
                        "source_type": "limitation",
                    }
                ],
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
        routes_backend.report_service,
        "generate_reports",
        lambda session: _outcome([9]),
    )

    response = client.post("/reports")

    assert response.status_code == 200
    body = response.json()
    assert (body["clusters"], body["count"]) == ([9], 1)
    assert body["report_generation"]["status"] == "complete"


def test_post_reports_returns_502_and_never_leaks_the_exception_on_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(session):
        raise RuntimeError("secret internal detail: db password=hunter2")

    monkeypatch.setattr(routes_backend.report_service, "generate_reports", _boom)

    response = client.post("/reports")

    assert response.status_code == 502
    assert "hunter2" not in response.text
    assert response.json() == {"detail": "Report generation failed"}


# --------------------------------------------------------------------------
# Stage 9 provider failures: each class of failure needs its own answer, since
# "retry later" and "fix your key" are different instructions to an operator.
# --------------------------------------------------------------------------


def _reports_raising(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def _raise(session, *args, **kwargs):
        raise exc

    monkeypatch.setattr(routes_backend.report_service, "generate_reports", _raise)


def test_post_reports_answers_429_with_retry_after_when_groq_meters_us(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The evidence base is intact and only generation was refused, so the
    caller is told to come back — with the provider's own wait, not a guess."""
    _reports_raising(
        monkeypatch,
        LLMRateLimitError(
            "Groq rate limit reached (TPM): Limit 8000.", retry_after=42.0
        ),
    )

    response = client.post("/reports")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "42"
    assert "TPM" in response.json()["detail"]


def test_a_rate_limit_without_a_named_wait_still_answers_429(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reports_raising(monkeypatch, LLMRateLimitError("Groq rate limit reached."))

    response = client.post("/reports")

    assert response.status_code == 429
    assert "Retry-After" not in response.headers


def test_post_reports_answers_502_for_a_rejected_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not 503: waiting will not fix a credential, and 503 invites a retry."""
    _reports_raising(
        monkeypatch, LLMAuthError("Groq rejected the configured GROQ_API_KEY.")
    )

    response = client.post("/reports")

    assert response.status_code == 502
    assert "GROQ_API_KEY" in response.json()["detail"]


def test_post_reports_answers_502_for_an_unusable_model(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reports_raising(
        monkeypatch,
        LLMConfigurationError("Groq rejected the request — check GROQ_MODEL."),
    )

    response = client.post("/reports")

    assert response.status_code == 502
    assert "GROQ_MODEL" in response.json()["detail"]


def test_post_reports_answers_503_for_a_genuine_outage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reports_raising(monkeypatch, LLMUnavailableError("Groq is unavailable."))

    response = client.post("/reports")

    assert response.status_code == 503


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
        # `graph_snapshot` always returns all five keys; `/graph` is typed
        # against that shape.
        lambda neo4j, limit: {
            "nodes": [],
            "edges": [],
            "total_nodes": 0,
            "total_edges": 0,
            "truncated": False,
        },
    )

    response = client.get("/graph")

    assert response.status_code == 200
    assert response.json() == {
        "nodes": [],
        "edges": [],
        "total_nodes": 0,
        "total_edges": 0,
        "truncated": False,
    }


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
        "generate_reports",
        lambda session, settings=None, **kwargs: _outcome([1]),
    )

    response = client.post("/pipeline/run", json={"generate_reports": True})

    assert response.status_code == 200
    body = response.json()
    assert body["parsed"] == 1
    assert body["extracted"] == 2
    assert body["indexed"] == 3
    assert body["reports"] == [1]


def test_pipeline_run_reports_s2_outcomes_that_are_not_clean_parses(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PDF no parser read, and a parse made while GROBID was down, are named.

    Both used to be returned as plain "parsed" ids, indistinguishable from a
    GROBID parse of a readable PDF (D-S2-STATUS-01). Additive keys.
    """
    _stub_evidence_stages(monkeypatch, [])
    monkeypatch.setattr(
        routes_backend,
        "parse_all_pending",
        lambda *a, **k: SimpleNamespace(
            processed=[1, 2, 3], missing_pdfs=[], no_text=[2], grobid_unavailable=[3]
        ),
    )

    body = client.post("/pipeline/run", json={"generate_reports": False}).json()

    assert body["parsed"] == [1, 2, 3]
    assert body["no_text_pdfs"] == [2]
    assert body["grobid_unavailable"] == [3]


def test_a_rate_limited_pipeline_run_keeps_the_evidence_base_and_says_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2-S8 succeeded and only S9 was metered.

    The answer has to say the evidence base survived and name the S9-only
    retry, or the operator's next move is to rebuild a corpus that was never
    broken. 429 rather than 503: nothing is wrong with the deployment.
    """
    _stub_evidence_stages(monkeypatch, [])

    def _metered(session, settings=None, **kwargs):
        raise LLMRateLimitError("Groq rate limit reached (TPM).", retry_after=30.0)

    monkeypatch.setattr(routes_backend.report_service, "generate_reports", _metered)

    response = client.post("/pipeline/run", json={"generate_reports": True})

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "30"
    detail = response.json()["detail"]
    assert "evidence base was built" in detail
    assert "POST /reports" in detail


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
        "generate_reports",
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

    def _reports(session, settings=None, **kwargs):
        events.append("reports")
        raise error

    monkeypatch.setattr(routes_backend.report_service, "generate_reports", _reports)

    response = client.post("/pipeline/run", json={"generate_reports": True})

    assert response.status_code == status_code
    assert str(error) in response.json()["detail"]
    assert "commit" in events[events.index("rankings") + 1 : events.index("reports")]


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

    monkeypatch.setattr(routes_backend.report_service, "generate_reports", _fail)

    response = client.post("/reports")

    assert response.status_code == status_code
    assert response.json() == {"detail": str(error)}


# --------------------------------------------------------------------------
# DELETE /admin/corpus
# --------------------------------------------------------------------------


def test_admin_corpus_reset_rejects_requests_without_api_key(
    unauthenticated_client: TestClient,
) -> None:
    response = unauthenticated_client.delete("/admin/corpus")

    assert response.status_code == 401


def test_admin_corpus_reset_calls_shared_reset_and_returns_its_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routes_backend, "graph_session", lambda *a, **k: _NullContext(object())
    )
    monkeypatch.setattr(routes_backend, "EvidenceIndex", lambda settings: object())
    captured = {}

    def _fake_reset(session, neo4j, index, pdf_storage_path=None):
        captured["called"] = True
        return {
            "deleted": True,
            "papers": 3,
            "pdfs": 2,
            "postgres_records": {"papers": 3},
            "neo4j_nodes": 5,
            "neo4j_relationships": 4,
            "qdrant_points": 7,
        }

    monkeypatch.setattr(routes_backend, "reset_corpus", _fake_reset)

    response = client.delete("/admin/corpus")

    assert response.status_code == 200
    assert captured["called"] is True
    body = response.json()
    assert body["deleted"] is True
    assert body["papers"] == 3
    assert body["pdfs"] == 2
    assert body["neo4j_nodes"] == 5
    assert body["qdrant_points"] == 7
    assert "run_id" in body


def test_admin_corpus_reset_surfaces_failure_instead_of_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routes_backend, "graph_session", lambda *a, **k: _NullContext(object())
    )
    monkeypatch.setattr(routes_backend, "EvidenceIndex", lambda settings: object())

    def _fail(session, neo4j, index, pdf_storage_path=None):
        raise RuntimeError("neo4j://admin:hunter2@internal-host offline")

    monkeypatch.setattr(routes_backend, "reset_corpus", _fail)

    response = client.delete("/admin/corpus")

    assert response.status_code == 502
    assert "hunter2" not in response.text
    assert response.json() == {"detail": "Corpus reset failed"}


def test_admin_corpus_reset_rejects_lock_contention(
    client: TestClient, db_session: SASession
) -> None:
    from app.core.pipeline import writer_lock

    with writer_lock(db_session):
        response = client.delete("/admin/corpus")

    assert response.status_code == 409


# ---------------------------------------------------------------------------
# D-09-08: S9 says how it went, and a partial pass is not marked complete
# ---------------------------------------------------------------------------


def _outcome(stored, failures=(), requested=None, reused=()):
    from app.explanation.service import ReportOutcome

    failures = [{"cluster_id": cid, "reason": reason} for cid, reason in failures]
    return ReportOutcome(
        requested=requested if requested is not None else len(stored) + len(failures),
        stored=list(stored),
        reused=list(reused),
        failures=failures,
    )


def _scripted_reports(monkeypatch, outcomes):
    """Patch S9 with a queue of outcomes, recording each call's `resume`."""
    calls: list[dict] = []

    def _generate(session, settings=None, index=None, llm=None, *, resume=False):
        calls.append({"resume": resume})
        return outcomes[len(calls) - 1]

    monkeypatch.setattr(routes_backend.report_service, "generate_reports", _generate)
    return calls


BASELINE_PARTIAL = _outcome(
    [11, 2],
    failures=[
        (5, "Groq's structured output did not validate against the report schema"),
        (9, "Groq's response was truncated by max_completion_tokens (2048)"),
        (7, "Groq's structured output did not validate against the report schema"),
    ],
)


def test_a_partial_s9_pass_is_reported_as_partial_not_as_success(client, monkeypatch):
    """The baseline's exact shape: 5 requested, 2 stored, 3 failed."""
    _stub_evidence_stages(monkeypatch, [])
    _scripted_reports(monkeypatch, [BASELINE_PARTIAL])

    response = client.post("/pipeline/run", json={"generate_reports": True})

    assert response.status_code == 200
    body = response.json()
    assert body["reports"] == [11, 2]
    summary = body["report_generation"]
    assert summary["status"] == "partial"
    assert (summary["requested"], summary["succeeded"], summary["failed"]) == (5, 2, 3)
    assert [f["cluster_id"] for f in summary["failures"]] == [5, 9, 7]
    assert "truncated" in summary["failures"][1]["reason"]


def test_a_failed_s9_pass_is_reported_as_failed(client, monkeypatch):
    _stub_evidence_stages(monkeypatch, [])
    _scripted_reports(monkeypatch, [_outcome([], failures=[(1, "did not validate")])])

    body = client.post("/pipeline/run", json={"generate_reports": True}).json()

    assert body["report_generation"]["status"] == "failed"
    assert body["reports"] == []


def test_a_complete_s9_pass_is_reported_as_complete(client, monkeypatch):
    _stub_evidence_stages(monkeypatch, [])
    _scripted_reports(monkeypatch, [_outcome([1])])

    body = client.post("/pipeline/run", json={"generate_reports": True}).json()

    assert body["report_generation"]["status"] == "complete"
    assert body["report_generation"]["failed"] == 0


def test_a_run_without_reports_says_none_were_requested(client, monkeypatch):
    _stub_evidence_stages(monkeypatch, [])

    body = client.post("/pipeline/run", json={"generate_reports": False}).json()

    assert body["report_generation"]["status"] == "not_requested"


def test_a_partial_pass_is_resumed_by_the_next_run(client, monkeypatch):
    """The baseline marked S9 current after 2 of 5; the next run would have
    served those two and never retried the other three."""
    _stub_evidence_stages(monkeypatch, [])
    calls = _scripted_reports(
        monkeypatch,
        [BASELINE_PARTIAL, _outcome([11, 2, 5, 9, 7], reused=[11, 2])],
    )

    client.post("/pipeline/run", json={"generate_reports": True})
    second = client.post("/pipeline/run", json={"generate_reports": True}).json()

    assert calls == [{"resume": False}, {"resume": True}]
    assert second["report_generation"]["status"] == "complete"
    assert second["report_generation"]["reused"] == 2


def test_a_complete_pass_is_not_regenerated_by_the_next_run(client, monkeypatch):
    _stub_evidence_stages(monkeypatch, [])
    calls = _scripted_reports(monkeypatch, [_outcome([1])])

    client.post("/pipeline/run", json={"generate_reports": True})
    second = client.post("/pipeline/run", json={"generate_reports": True}).json()

    assert len(calls) == 1
    assert second["report_generation"]["status"] == "complete"


def test_a_failed_pass_is_retried_in_full(client, monkeypatch):
    """Nothing was stored, so there is nothing to resume from."""
    _stub_evidence_stages(monkeypatch, [])
    calls = _scripted_reports(
        monkeypatch, [_outcome([], failures=[(1, "x")]), _outcome([1])]
    )

    client.post("/pipeline/run", json={"generate_reports": True})
    client.post("/pipeline/run", json={"generate_reports": True})

    assert calls == [{"resume": False}, {"resume": False}]


def test_an_s9_marker_written_before_outcomes_existed_is_not_current(
    client, db_session, monkeypatch
):
    """The baseline database holds an `s9` marker written by the old rule
    (current after any success, even 2 of 5). It must not stop the next run
    from generating the reports that failed."""
    from app.core.pipeline import DerivedState, fingerprint

    _stub_evidence_stages(monkeypatch, [])
    client.post("/pipeline/run", json={"generate_reports": False})
    evidence_digest = db_session.get(DerivedState, "s6-s8").input_fingerprint
    # Exactly what the pre-outcome code stored for this evidence and settings
    # (the stub settings carry no `report_`/`groq_model` keys).
    db_session.merge(
        DerivedState(
            name="s9",
            input_fingerprint=fingerprint([evidence_digest, {}, "prompt-v1"]),
        )
    )
    db_session.commit()
    calls = _scripted_reports(monkeypatch, [_outcome([1])])

    client.post("/pipeline/run", json={"generate_reports": True})

    assert len(calls) == 1


def test_post_reports_returns_the_outcome_additively(client, monkeypatch):
    monkeypatch.setattr(
        routes_backend.report_service,
        "generate_reports",
        lambda session: BASELINE_PARTIAL,
    )

    body = client.post("/reports").json()

    assert body["clusters"] == [11, 2]
    assert body["count"] == 2
    assert body["report_generation"]["status"] == "partial"


def test_derived_state_from_before_one_statement_per_sentence_is_not_current(
    db_session: SASession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-06-14 changed what S6 produces from unchanged inputs.

    Clusters built from label rows must be recomputed, not reused because the
    corpus fingerprint happens to be the same.
    """
    assert routes_backend._DERIVED_VERSION >= 2
    settings = SimpleNamespace()
    current = routes_backend._derived_fingerprint(db_session, settings)
    monkeypatch.setattr(routes_backend, "_DERIVED_VERSION", 1)
    assert routes_backend._derived_fingerprint(db_session, settings) != current


def test_gaps_trends_and_clusters_carry_their_evidence_qualifiers(
    client: TestClient, db_session: SASession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Additive keys: single-paper scope (S8) and trend sample caveats (S7)."""
    import datetime

    cluster_id = _seed_cluster(db_session)
    replace_trends(
        db_session, [(cluster_id, TrendSeries({"2025": 1, "2026": 2}, 1.0, 0))]
    )
    replace_rankings(
        db_session,
        [(cluster_id, -24.0, GapSignals(1, 0.0, 1.0, 0.0, 0.0))],
    )
    db_session.commit()
    monkeypatch.setattr(routes_backend, "_today", lambda: datetime.date(2026, 10, 13))

    [gap] = client.get("/gaps").json()
    assert (gap["paper_count"], gap["evidence_scope"], gap["exploratory"]) == (
        1,
        "single_paper",
        True,
    )
    [trend] = client.get("/trends").json()
    assert trend["qualifiers"]["partial_years"] == ["2026"]
    assert trend["qualifiers"]["small_sample"] is True
    assert trend["slope"] == 1.0  # the stored value is unchanged
    [cluster] = client.get("/clusters").json()
    assert cluster["paper_count"] == len({m["paper_id"] for m in cluster["members"]})
    assert cluster["evidence_scope"] in {"single_paper", "corroborated"}
