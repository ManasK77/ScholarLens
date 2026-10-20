"""Typed response contracts for `/clusters`, `/reports` and `/graph`.

Each test compares what the HTTP layer serves (through the declared
`response_model`) with what the route function itself returns when called
directly. Equality proves the model neither adds, drops, renames nor
re-types anything, for populated, empty, null-field and archived-snapshot
data. Fixture rows mirror the shapes found in the live database (read-only
survey, 2026-10-18): claims with and without `basis` / `llm_supported`,
justification `None` on old rows, evidence without derived keys in snapshots.
"""

import json
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SASession
from sqlalchemy.pool import StaticPool

import app.main  # noqa: F401 -- registers every table on Base.metadata
from app.api import routes_backend
from app.api.security import require_api_key
from app.clustering.storage import Cluster, ClusterContradiction, ClusterMember
from app.explanation.schema import MAX_EVIDENCE_CHARS
from app.explanation.storage import GapReport, ReportSnapshot
from app.ingestion import corpus


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    corpus.Base.metadata.create_all(engine)
    with SASession(engine) as db:
        yield db


@pytest.fixture()
def client(session):
    application = FastAPI()
    application.include_router(routes_backend.router)
    application.dependency_overrides[require_api_key] = lambda: None
    application.dependency_overrides[corpus.get_session] = lambda: session
    return TestClient(application)


def _wire(value):
    """What the route's raw return value looks like as JSON."""
    return json.loads(json.dumps(value, default=str))


def _assert_types(value, expected: dict) -> None:
    for key, kinds in expected.items():
        assert isinstance(value[key], kinds), (key, value[key])


# --------------------------------------------------------------------------
# /clusters
# --------------------------------------------------------------------------


def _clusters(session) -> None:
    session.add_all(
        [
            Cluster(
                id=1,
                label="Multilingual evaluation",
                member_count=3,
                contradiction_flag=True,
                contradiction_score=0.91,
            ),
            Cluster(
                id=2,
                label=None,
                member_count=1,
                contradiction_flag=False,
                contradiction_score=0,
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            ClusterMember(
                cluster_id=1,
                point_id="p1",
                paper_id=10,
                year=2024,
                text="Results drop outside English [12].",
                source_type="limitation",
            ),
            # The same sentence under a second label: grouped into one entry.
            ClusterMember(
                cluster_id=1,
                point_id="p1b",
                paper_id=10,
                year=2024,
                text="Results drop outside English [12].",
                source_type="future_work",
            ),
            ClusterMember(
                cluster_id=1,
                point_id="p2",
                paper_id=11,
                year=None,
                text="Multilingual robustness holds.",
                source_type="limitation",
            ),
            ClusterMember(
                cluster_id=2,
                point_id="p3",
                paper_id=12,
                year=2023,
                text="One paper's view.",
                source_type="limitation",
            ),
            ClusterContradiction(
                cluster_id=1,
                left_point_id="p1",
                left_paper_id=10,
                right_point_id="p2",
                right_paper_id=11,
                score=0.91,
            ),
            # A pair whose right point is no longer a member: text is null.
            ClusterContradiction(
                cluster_id=1,
                left_point_id="p2",
                left_paper_id=11,
                right_point_id="gone",
                right_paper_id=13,
                score=0.8,
            ),
        ]
    )
    session.commit()


def test_clusters_response_equals_the_route_output(client, session) -> None:
    _clusters(session)

    served = client.get("/clusters").json()

    assert served == _wire(routes_backend.clusters(session))
    first = served[0]
    assert set(first) == {
        "id",
        "label",
        "member_count",
        "paper_count",
        "evidence_scope",
        "contradiction_flag",
        "contradiction_score",
        "contradictions",
        "members",
    }
    assert set(first["members"][0]) == {
        "point_id",
        "point_ids",
        "paper_id",
        "text",
        "year",
        "source_type",
        "source_types",
        "citation_markers",
    }
    assert set(first["contradictions"][0]) == {
        "left_point_id",
        "left_paper_id",
        "left_text",
        "right_point_id",
        "right_paper_id",
        "right_text",
        "score",
    }
    _assert_types(
        first,
        {
            "id": int,
            "member_count": int,
            "paper_count": int,
            "contradiction_flag": bool,
            "contradiction_score": float,
        },
    )
    assert first["members"][0]["source_types"] == ["future_work", "limitation"]
    assert first["members"][0]["citation_markers"] == ["[12]"]
    assert first["members"][1]["year"] is None
    assert first["contradictions"][1]["right_text"] is None
    assert served[1]["label"] is None and served[1]["contradictions"] == []
    assert served[1]["evidence_scope"] == "single_paper"


def test_clusters_empty(client) -> None:
    assert client.get("/clusters").json() == []


# --------------------------------------------------------------------------
# /reports
# --------------------------------------------------------------------------


def _evidence(point_id: str, paper_id: int, **overrides) -> dict:
    item = {
        "id": point_id,
        "paper_id": paper_id,
        "title": "A paper",
        "text": "Evaluation is English-only [3].",
        "year": 2024,
        "source_type": "limitation",
    }
    item.update(overrides)
    return item


VERIFIED_JUSTIFICATION = {
    "problem_evidence": {
        "supported": True,
        "statement": "The problem exists.",
        "evidence_ids": ["e1"],
        "basis": "llm_cited",
    },
    "solution_insufficiency_evidence": {
        "supported": False,
        "statement": "",
        "evidence_ids": [],
        "basis": "not_supported",
    },
    "research_gap_evidence": {
        "supported": True,
        "statement": "A gap remains.",
        "evidence_ids": ["e2"],
        "basis": "llm_cited",
    },
    "conflicting_evidence": {
        "supported": False,
        "statement": "Papers disagree.",
        "evidence_ids": ["e1", "e2"],
        "basis": "llm_unverified",
        "llm_supported": True,
        "verification": {
            "method": "nli",
            "threshold": 0.9,
            "pairs_checked": 1,
            "max_score": 0.42,
            "verified": False,
        },
    },
}


def _clusters_for_reports(session) -> None:
    session.add_all(
        [
            Cluster(
                id=i,
                label=f"c{i}",
                member_count=1,
                contradiction_flag=False,
                contradiction_score=0.0,
            )
            for i in (1, 2)
        ]
    )
    session.flush()


def test_current_reports_response_equals_the_route_output(client, session) -> None:
    _clusters_for_reports(session)
    session.add_all(
        [
            GapReport(
                cluster_id=1,
                gap="Multilingual defenses are untested.",
                evidence=[
                    _evidence("e1", 10),
                    _evidence(
                        "e2", 11, title=None, year=None, text="x" * MAX_EVIDENCE_CHARS
                    ),
                ],
                confidence=0.6,
                suggested_directions=["Benchmark", "Defense"],
                justification=VERIFIED_JUSTIFICATION,
                provider="groq",
                model="m",
            ),
            # A row stored before justifications existed: served as `{}`.
            GapReport(
                cluster_id=2,
                gap="Older report.",
                evidence=[_evidence("e3", 12)],
                confidence=1,
                suggested_directions=["Study"],
                justification=None,
                provider="groq",
                model="m",
            ),
        ]
    )
    session.commit()

    served = client.get("/reports").json()

    assert served == _wire(routes_backend.reports(session))
    first, second = served
    assert set(first) == {
        "cluster_id",
        "gap",
        "evidence",
        "confidence",
        "suggested_directions",
        "provider",
        "model",
        "justification",
        "evidence_summary",
    }
    assert set(first["evidence"][0]) == {
        "id",
        "paper_id",
        "title",
        "text",
        "year",
        "source_type",
        "citation_markers",
        "truncated",
    }
    assert first["evidence"][1]["title"] is None
    assert first["evidence"][1]["truncated"] is True
    conflict = first["justification"]["conflicting_evidence"]
    assert conflict["llm_supported"] is True
    assert conflict["verification"]["max_score"] == 0.42
    assert "llm_supported" not in first["justification"]["problem_evidence"]
    assert second["justification"] == {}
    assert first["evidence_summary"] == {
        "passage_count": 2,
        "unique_paper_count": 2,
        "max_passages_from_one_paper": 1,
    }
    assert "stale" not in first


def test_archived_snapshot_fallback_equals_the_route_output(client, session) -> None:
    stored = [
        {
            "cluster_id": 7,
            "gap": "Archived gap.",
            "evidence": [_evidence("e9", 20)],
            "confidence": 0.4,
            "suggested_directions": ["Revisit"],
            "provider": "groq",
            "model": "m",
            # Pre-basis claims, as in the oldest live snapshot.
            "justification": {
                "problem_evidence": {
                    "supported": True,
                    "statement": "s",
                    "evidence_ids": ["e9"],
                },
            },
        },
        {
            "cluster_id": 8,
            "gap": "Older still.",
            "evidence": [],
            "confidence": 0.1,
            "suggested_directions": ["x"],
            "provider": "groq",
            "model": "m",
            "justification": None,
        },
    ]
    session.add(
        ReportSnapshot(
            id="snap-1",
            reports=stored,
            run_id=None,
            created_at=datetime(2026, 10, 1, 12, tzinfo=UTC).replace(tzinfo=None),
        )
    )
    session.commit()

    served = client.get("/reports").json()

    assert served == _wire(routes_backend.reports(session))
    assert served[0]["stale"] is True
    assert served[0]["snapshot_id"] == "snap-1"
    assert served[0]["snapshot_created_at"] == "2026-10-01T12:00:00"
    assert "evidence_summary" not in served[0]
    assert "citation_markers" not in served[0]["evidence"][0]
    assert served[1]["justification"] is None


def test_reports_serves_an_older_archived_snapshot_without_a_500(
    client, session
) -> None:
    """Rows archived before justifications (and before every evidence key) existed.

    The oldest shape has no `justification` key and evidence items without
    `title`, `year` or `source_type`. Each is optional on the wire and omitted
    when the stored row never had it ("keys a row does not have are omitted").
    """
    stored = [
        {
            "cluster_id": 3,
            "gap": "Archived before justifications.",
            "evidence": [{"id": "e1", "paper_id": 5, "text": "Old passage."}],
            "confidence": 0.5,
            "suggested_directions": ["Revisit"],
            "provider": "groq",
            "model": "m",
        }
    ]
    session.add(
        ReportSnapshot(
            id="snap-old",
            reports=stored,
            run_id=None,
            created_at=datetime(2026, 9, 1, 12, tzinfo=UTC).replace(tzinfo=None),
        )
    )
    session.commit()

    response = client.get("/reports")

    assert response.status_code == 200
    (served,) = response.json()
    assert served["stale"] is True and served["snapshot_id"] == "snap-old"
    assert "justification" not in served
    assert served["evidence"] == [{"id": "e1", "paper_id": 5, "text": "Old passage."}]
    assert served["gap"] == "Archived before justifications."


def test_reports_empty(client) -> None:
    assert client.get("/reports").json() == []


# --------------------------------------------------------------------------
# /graph (the real graph_snapshot over a fake Neo4j session)
# --------------------------------------------------------------------------


class _FakeNeo4j:
    def __init__(self, nodes, edges, totals) -> None:
        self.nodes, self.edges, self.totals = nodes, edges, totals

    def run(self, query: str, **params):
        if "RETURN elementId(n) AS id" in query:
            return [dict(n) for n in self.nodes[: params["limit"]]]
        if "RETURN elementId(a) AS source" in query:
            return [dict(e) for e in self.edges]
        return [self.totals] if self.totals is not None else []


def _graph(monkeypatch, fake) -> None:
    @contextmanager
    def _session(*_args):
        yield fake

    monkeypatch.setattr(routes_backend, "graph_session", _session)


def test_graph_response_equals_the_route_output(client, monkeypatch) -> None:
    fake = _FakeNeo4j(
        nodes=[
            {"id": "4:a:1", "labels": ["Paper"], "label": "A paper"},
            {"id": "4:a:2", "labels": ["Entity", "Model"], "label": "BERT"},
            {"id": "4:a:3", "labels": [], "label": None},
        ],
        edges=[{"source": "4:a:1", "target": "4:a:2", "type": "MENTIONS"}],
        totals={"nodes": 40, "edges": 90},
    )
    _graph(monkeypatch, fake)

    served = client.get("/graph", params={"limit": 3}).json()

    assert served == _wire(routes_backend.get_graph(limit=3))
    assert set(served) == {"nodes", "edges", "total_nodes", "total_edges", "truncated"}
    assert set(served["nodes"][0]) == {"id", "labels", "label"}
    assert set(served["edges"][0]) == {"source", "target", "type"}
    assert served["nodes"][2]["label"] is None
    assert served["truncated"] is True


def test_graph_empty(client, monkeypatch) -> None:
    _graph(monkeypatch, _FakeNeo4j(nodes=[], edges=[], totals={"nodes": 0, "edges": 0}))

    assert client.get("/graph").json() == {
        "nodes": [],
        "edges": [],
        "total_nodes": 0,
        "total_edges": 0,
        "truncated": False,
    }


# --------------------------------------------------------------------------
# OpenAPI
# --------------------------------------------------------------------------


def test_openapi_registers_the_three_response_models(client) -> None:
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]

    def schema_of(path: str) -> dict:
        return spec["paths"][path]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]

    assert schema_of("/clusters")["items"]["$ref"].endswith("/ClusterOut")
    assert schema_of("/reports")["items"]["$ref"].endswith("/ReportOut")
    assert schema_of("/graph")["$ref"].endswith("/GraphOut")
    for name in (
        "ClusterMemberOut",
        "ContradictionPairOut",
        "ReportEvidenceOut",
        "ReportJustification",
        "JustificationClaim",
        "GraphNodeOut",
    ):
        assert name in schemas, name


def test_a_malformed_stored_row_is_a_sanitized_500_not_leaked_data(monkeypatch) -> None:
    """Typed responses turn an impossible shape into an error, never into a
    response carrying the validator's view of internal data."""
    from app.main import app as main_app

    monkeypatch.setenv("API_KEY", "contract-dummy-key-4d1a")
    for key, value in {
        "NEO4J_URI": "bolt://n:7687",
        "NEO4J_USER": "n",
        "NEO4J_PASSWORD": "p",
        "QDRANT_URL": "http://q:6333",
        "POSTGRES_HOST": "p",
        "POSTGRES_PORT": "5432",
        "POSTGRES_USER": "u",
        "POSTGRES_PASSWORD": "p",
        "POSTGRES_DB": "d",
        "PDF_STORAGE_PATH": "/tmp",
        "MAX_UPLOAD_SIZE_MB": "5",
        "UNPAYWALL_EMAIL": "a@example.com",
        "GROBID_URL": "http://g:8070",
    }.items():
        monkeypatch.setenv(key, value)
    _graph(monkeypatch, None)
    monkeypatch.setattr(
        routes_backend,
        "graph_snapshot",
        lambda neo4j, limit: {"nodes": [{"id": "internal-secret-node"}], "edges": []},
    )

    response = TestClient(main_app, raise_server_exceptions=False).get(
        "/graph", headers={"X-API-Key": "contract-dummy-key-4d1a"}
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "internal-secret-node" not in response.text
