"""/collect through the S1 final relevance gate (D-S1-GATE-01).

Every candidate a source returns gets a recorded decision -- admitted,
rejected, or capped -- in `candidate_decisions`, under a `collection_runs`
row that stores the topic. Only admitted candidates reach `papers`; rejected
ones never take a source slot; admitted ones are ranked by the gate's score,
not by the order the source returned them.

The gate's logic is the production logic; only the semantic scorer is fake.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import routes_ingestion
from app.ingestion import corpus
from app.ingestion.collection_audit import CandidateDecision, CollectionRun
from app.ingestion.corpus import Base, Paper
from app.ingestion.sources import acl_anthology, arxiv, semantic_scholar
from app.main import app
from tests.ingestion.gate_fakes import FakeScorer, real_gate

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
    "API_KEY": "test-api-key-ingestion",
}
AUTH_HEADERS = {"X-API-Key": REQUIRED_ENV["API_KEY"]}
TOPIC = "multilingual prompt injection"


@pytest.fixture(autouse=True)
def _settings_env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PDF_STORAGE_PATH", str(tmp_path))


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def client(db: Session):
    def _override():
        yield db

    app.dependency_overrides[corpus.get_session] = _override
    yield TestClient(app, headers=AUTH_HEADERS)
    app.dependency_overrides.clear()


def _gate(monkeypatch, scorer=None):
    scorer = scorer or FakeScorer()
    monkeypatch.setattr(
        routes_ingestion, "build_gate", lambda topic, settings: real_gate(topic, scorer)
    )
    return scorer


def _sources(monkeypatch, *, arxiv_results=(), s2_results=(), acl_results=()):
    seen: dict[str, dict] = {}

    def _arxiv(topic, **kwargs):
        seen["arxiv"] = kwargs
        return list(arxiv_results)

    def _s2(topic, api_key=None, **kwargs):
        seen["semantic_scholar"] = kwargs
        return list(s2_results)

    def _acl(bib_path, topic, **kwargs):
        seen["acl_anthology"] = kwargs
        return list(acl_results)

    monkeypatch.setattr(arxiv, "search_topic", _arxiv)
    monkeypatch.setattr(arxiv, "fetch_pdf", lambda candidate, dest_dir, **kw: None)
    monkeypatch.setattr(semantic_scholar, "search_topic", _s2)
    monkeypatch.setattr(
        acl_anthology, "download_anthology_export", lambda dest_path, force=False: None
    )
    monkeypatch.setattr(acl_anthology, "search_topic", _acl)
    monkeypatch.setattr(acl_anthology, "fetch_pdf", lambda candidate, dest_dir: None)
    monkeypatch.setattr(
        routes_ingestion.unpaywall, "enrich", lambda session, email, dest_dir, **kw: []
    )
    return seen


def _arxiv(n, title, abstract="", year=2025, doi=None):
    return {
        "title": title,
        "authors": ["A"],
        "year": year,
        "venue": None,
        "doi": doi,
        "abstract": abstract,
        "arxiv_id": f"2501.{n:05d}",
        "pdf_url": f"https://arxiv.org/pdf/2501.{n:05d}",
    }


RELEVANT = "Multilingual Prompt Injection Study"
JAILBREAK = "Multilingual Jailbreak Study"


def _decisions(db, **filters):
    statement = select(CandidateDecision).order_by(CandidateDecision.id)
    for key, value in filters.items():
        statement = statement.where(getattr(CandidateDecision, key) == value)
    return list(db.scalars(statement))


# ---------------------------------------------------------------------------
# Rejections never cost a slot, and never reach the corpus
# ---------------------------------------------------------------------------


def test_rejected_candidates_do_not_consume_the_source_cap(client, db, monkeypatch):
    _gate(monkeypatch)
    results = [
        _arxiv(i, f"{JAILBREAK} {i}", "Jailbreaks in Hindi and Tamil.")
        for i in range(12)
    ]
    results += [
        _arxiv(100 + i, f"{RELEVANT} {i}", "Injected instructions.") for i in range(10)
    ]
    _sources(monkeypatch, arxiv_results=results)

    body = client.post("/collect", json={"topic": TOPIC}).json()

    stored = db.scalars(select(Paper).where(Paper.source == "arxiv")).all()
    assert sorted(p.title for p in stored) == sorted(
        f"{RELEVANT} {i}" for i in range(10)
    )
    rejected = _decisions(db, decision="rejected")
    assert len(rejected) == 12
    assert {d.reason_code for d in rejected} == {"missing_facet:prompt injection"}
    assert all(d.paper_id is None for d in rejected)
    assert body["rejected"]["by_reason"] == {"missing_facet:prompt injection": 12}
    assert body["source_stats"]["arxiv"]["rejected"] == 12


def test_the_error_injection_paper_is_recorded_and_not_admitted(
    client, db, monkeypatch
):
    _gate(monkeypatch)
    paper_18 = {
        "title": "A Comprehensive Evaluation of Multilingual Chain-of-Thought Reasoning",
        "authors": ["B"],
        "year": 2026,
        "venue": "Findings of the Association for Computational Linguistics: EACL 2026",
        "doi": "10.18653/v1/2026.findings-eacl.276",
        "abstract": "We use prompt perturbations, truncation and error injection "
        "to probe faithfulness across languages.",
        "acl_id": "2026.findings-eacl.276",
        "pdf_url": "https://aclanthology.org/2026.findings-eacl.276.pdf",
    }
    _sources(monkeypatch, acl_results=[paper_18])

    client.post("/collect", json={"topic": TOPIC})

    assert db.scalars(select(Paper)).all() == []
    [decision] = _decisions(db)
    assert decision.decision == "rejected"
    assert decision.reason_code == "sense_mismatch:prompt injection"
    assert decision.source == "acl_anthology"
    assert decision.source_ref == "2026.findings-eacl.276"
    assert decision.evidence["negative"]["prompt injection"] == ["error injection"]


# ---------------------------------------------------------------------------
# Ranking by gate score, not by source order
# ---------------------------------------------------------------------------


def test_admitted_candidates_are_ranked_by_gate_score_before_the_cap(
    client, db, monkeypatch
):
    """The source returns its weakest matches first; the cap keeps the best."""
    scores = {f"{RELEVANT} {i}": float(i) for i in range(12)}
    _gate(monkeypatch, FakeScorer(by_title=scores))
    _sources(
        monkeypatch,
        arxiv_results=[_arxiv(i, f"{RELEVANT} {i}", "Abstract.") for i in range(12)],
    )

    body = client.post("/collect", json={"topic": TOPIC}).json()

    stored = {p.title for p in db.scalars(select(Paper))}
    assert stored == {f"{RELEVANT} {i}" for i in range(2, 12)}
    capped = _decisions(db, decision="capped")
    assert sorted(d.title for d in capped) == [f"{RELEVANT} 0", f"{RELEVANT} 1"]
    assert {d.reason_code for d in capped} == {"source_cap_reached"}
    assert body["capped"] == 2


# ---------------------------------------------------------------------------
# Every candidate is accounted for, under a run that records the topic
# ---------------------------------------------------------------------------


def test_every_candidate_gets_exactly_one_recorded_decision(client, db, monkeypatch):
    _gate(monkeypatch)
    _sources(
        monkeypatch,
        arxiv_results=[
            _arxiv(1, RELEVANT, "Abstract."),
            _arxiv(2, JAILBREAK, "Jailbreaks across languages."),
        ],
        s2_results=[
            {"title": "Unrelated", "authors": [], "year": 2024, "abstract": "Nothing."}
        ],
    )

    body = client.post("/collect", json={"topic": TOPIC}).json()

    decisions = _decisions(db)
    assert len(decisions) == 3
    assert all(d.reason for d in decisions)
    assert {d.run_id for d in decisions} == {body["collection_run_id"]}
    [admitted] = [d for d in decisions if d.decision == "admitted"]
    assert admitted.paper_id == db.scalars(select(Paper.id)).one()


def test_the_collection_run_persists_the_topic_and_the_gate_configuration(
    client, db, monkeypatch
):
    _gate(monkeypatch)
    _sources(monkeypatch, arxiv_results=[_arxiv(1, RELEVANT, "Abstract.")])

    body = client.post("/collect", json={"topic": TOPIC}).json()

    run = db.get(CollectionRun, body["collection_run_id"])
    assert run.kind == "collect"
    assert run.topic == TOPIC
    assert run.gate_version
    assert run.lexicon_version
    assert [f["name"] for f in run.facets] == ["multilingual", "prompt injection"]
    assert run.settings["semantic_floor"] == -1.0
    assert run.source_status["arxiv"] == "ok"
    assert run.counts["admitted"] == 1


def test_the_audit_trail_is_readable_over_the_api(client, db, monkeypatch):
    _gate(monkeypatch)
    _sources(
        monkeypatch,
        arxiv_results=[_arxiv(1, RELEVANT, "Abstract."), _arxiv(2, JAILBREAK, "")],
    )
    run_id = client.post("/collect", json={"topic": TOPIC}).json()["collection_run_id"]

    response = client.get(f"/collections/{run_id}/decisions")

    assert response.status_code == 200
    body = response.json()
    assert body["run"]["topic"] == TOPIC
    assert [d["decision"] for d in body["decisions"]] == ["admitted", "rejected"]
    assert body["decisions"][1]["reason_code"] == "insufficient_text"
    assert client.get("/collections/999999/decisions").status_code == 404


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


def test_a_cross_source_duplicate_of_an_admitted_paper_merges_and_says_why(
    client, db, monkeypatch
):
    """Semantic Scholar often has no abstract. Its copy of a paper arXiv
    already admitted is the same paper, not a new relevance question."""
    _gate(monkeypatch)
    _sources(
        monkeypatch,
        arxiv_results=[_arxiv(1, RELEVANT, "Abstract.", year=2025, doi="10.1/pi")],
        s2_results=[
            {
                "title": RELEVANT,
                "authors": ["A"],
                "year": 2025,
                "doi": "10.1/pi",
                "abstract": None,
                "citation_count": 42,
                "venue": "ACL",
            }
        ],
    )

    client.post("/collect", json={"topic": TOPIC})

    [paper] = db.scalars(select(Paper)).all()
    assert paper.citation_count == 42
    [s2] = _decisions(db, source="semantic_scholar")
    assert s2.decision == "admitted"
    assert s2.paper_id == paper.id


def test_a_duplicate_the_gate_would_reject_is_admitted_as_duplicate_of_admitted(
    client, db, monkeypatch
):
    _gate(monkeypatch)
    _sources(
        monkeypatch,
        arxiv_results=[_arxiv(1, RELEVANT, "Abstract.", doi="10.1/pi")],
        s2_results=[
            {"title": "Short title", "authors": [], "year": 2025, "doi": "10.1/pi"}
        ],
    )

    client.post("/collect", json={"topic": TOPIC})

    [s2] = _decisions(db, source="semantic_scholar")
    assert s2.decision == "admitted"
    assert s2.reason_code == "duplicate_of_admitted"
    assert s2.evidence["gate"]["reason_code"] == "insufficient_text"


def test_within_source_duplicates_are_stored_once(client, db, monkeypatch):
    _gate(monkeypatch)
    twin = {
        "title": RELEVANT,
        "authors": ["A"],
        "year": 2025,
        "abstract": "Abstract.",
        "pdf_url": "https://aclanthology.org/x.pdf",
    }
    _sources(
        monkeypatch,
        acl_results=[
            {**twin, "acl_id": "2025.acl-1.1"},
            {**twin, "acl_id": "2025.acl-1.2"},
        ],
    )

    client.post("/collect", json={"topic": TOPIC})

    assert len(db.scalars(select(Paper)).all()) == 1
    decisions = _decisions(db)
    assert [d.decision for d in decisions] == ["admitted", "admitted"]
    assert decisions[0].paper_id == decisions[1].paper_id


def test_a_rejected_candidate_never_modifies_an_existing_paper(client, db, monkeypatch):
    """A legacy row the gate never judged is not enriched by an irrelevant
    candidate that happens to share its DOI."""
    db.add(Paper(title="Legacy", year=2024, doi="10.1/legacy", source="arxiv"))
    db.commit()
    _gate(monkeypatch)
    _sources(
        monkeypatch,
        s2_results=[
            {
                "title": JAILBREAK,
                "year": 2024,
                "doi": "10.1/legacy",
                "abstract": "Jailbreaks.",
                "venue": "Somewhere",
            }
        ],
    )

    client.post("/collect", json={"topic": TOPIC})

    [paper] = db.scalars(select(Paper)).all()
    assert paper.venue is None
    [decision] = _decisions(db)
    assert decision.decision == "rejected"
    assert decision.evidence["existing_paper_id"] == paper.id


# ---------------------------------------------------------------------------
# Larger candidate pools, the same admitted cap
# ---------------------------------------------------------------------------


def test_sources_are_asked_for_a_larger_candidate_pool(client, db, monkeypatch):
    _gate(monkeypatch)
    seen = _sources(monkeypatch)

    client.post("/collect", json={"topic": TOPIC})

    assert seen["arxiv"]["max_results"] == 30
    assert seen["semantic_scholar"]["limit"] == 40
    assert seen["semantic_scholar"]["request_limit"] == 40
    assert callable(seen["acl_anthology"]["matches"])
    assert seen["acl_anthology"]["prefilter_terms"]


def test_the_acl_pool_is_bounded_and_the_overflow_is_counted(client, db, monkeypatch):
    # The pool is never below the cap, so the cap comes down with it.
    monkeypatch.setenv("ACL_ANTHOLOGY_MAX_RESULTS", "2")
    monkeypatch.setenv("ACL_ANTHOLOGY_CANDIDATE_POOL", "3")
    _gate(monkeypatch)
    many = [
        {
            "title": f"{RELEVANT} {i}",
            "year": 2025,
            "abstract": "Abstract.",
            "acl_id": f"2025.acl-{i}",
            "pdf_url": "https://aclanthology.org/x.pdf",
        }
        for i in range(5)
    ]
    _sources(monkeypatch, acl_results=many)

    body = client.post("/collect", json={"topic": TOPIC}).json()

    assert len(_decisions(db, source="acl_anthology")) == 3
    assert body["source_stats"]["acl_anthology"]["beyond_candidate_pool"] == 2


# ---------------------------------------------------------------------------
# Manual uploads: an owner decision, not a discovery decision
# ---------------------------------------------------------------------------


def test_an_upload_is_admitted_with_an_owner_upload_decision(client, db):
    response = client.post(
        "/upload",
        files=[("files", ("my paper.pdf", b"%PDF-1.4 content", "application/pdf"))],
    )

    [paper_id] = response.json()["created"]
    [decision] = _decisions(db)
    assert decision.decision == "admitted"
    assert decision.reason_code == "owner_upload"
    assert decision.source == "manual_upload"
    assert decision.source_ref == "my paper.pdf"
    assert decision.paper_id == paper_id
    run = db.get(CollectionRun, decision.run_id)
    assert run.kind == "upload"
    assert run.topic is None
