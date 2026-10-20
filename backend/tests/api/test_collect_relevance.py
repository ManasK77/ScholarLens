"""/collect through the S1 final relevance gate (D-S1-GATE-01).

Every candidate a source returns gets a recorded decision -- admitted,
rejected, or capped -- in `candidate_decisions`, under a `collection_runs`
row that stores the topic. Only admitted candidates reach `papers`; rejected
ones never take a source slot; admitted ones are ranked by
`relevance.rank_key`, not by the order the source returned them.

The gate's logic is the production logic; only the semantic scorer is fake.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import routes_ingestion
from app.ingestion import collection_audit, corpus, fulltext, unpaywall
from app.ingestion.collection_audit import CandidateDecision, CollectionRun
from app.ingestion.corpus import Base, Paper
from app.ingestion.pdf_store import DownloadResult
from app.ingestion.relevance import RelevanceDecision
from app.ingestion.sources import acl_anthology, arxiv, semantic_scholar
from app.ingestion.unpaywall import OaLookup
from app.main import app
from tests.ingestion.gate_fakes import FakeScorer, real_gate
from tests.ingestion.test_paper_language import ENGLISH_ABOUT_TAMIL, TAMIL_SCRIPT

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
        acl_anthology,
        "download_anthology_export",
        lambda dest_path, force=False, **kw: None,
    )
    monkeypatch.setattr(acl_anthology, "search_topic", _acl)
    monkeypatch.setattr(
        acl_anthology, "fetch_pdf", lambda candidate, dest_dir, **kw: None
    )
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

    # D-S1-RETRIEVAL-01: retrieval ceilings and page sizes, separate from
    # the cap of 10 new papers per source.
    assert seen["arxiv"]["max_results"] == 200
    assert seen["arxiv"]["page_size"] == 100
    assert seen["semantic_scholar"]["limit"] == 100
    assert seen["semantic_scholar"]["request_limit"] == 100
    assert seen["semantic_scholar"]["max_pages"] == 3
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


def test_arxiv_is_asked_with_the_gates_facet_synonyms(client, db, monkeypatch):
    _gate(monkeypatch)
    seen = _sources(monkeypatch)

    client.post("/collect", json={"topic": TOPIC})

    groups = seen["arxiv"]["query_groups"]
    assert "cross-lingual" in groups[0]
    # Lexicon 2026-10-12.2: the prompt-injection facet is searched as phrases.
    assert groups[1][:2] == ["prompt injection", "prompt injections"]
    assert len(groups) == 2


# ---------------------------------------------------------------------------
# The cap counts new papers only (D-CAP-04, 2026-10-12)
#
# Run 1: Semantic Scholar's ten slots held five papers arXiv had already
# stored, so it contributed five new papers and capped nine relevant ones.
# D-CAP-03 stopped same-run cross-source merges using a slot but kept one for
# papers stored by an earlier run; run 2 then spent Semantic Scholar slots on
# v1 admissions while relevant candidates were capped. The owner's rule is
# "at most 10 new papers per source per run": any merge onto an existing
# paper is recorded and merged but uses no slot.
# ---------------------------------------------------------------------------


def _s2(title, doi, abstract="Attacks written in Hindi and Tamil."):
    return {
        "title": title,
        "authors": ["A"],
        "year": 2025,
        "doi": doi,
        "abstract": abstract,
    }


def test_cross_source_duplicates_merge_without_using_the_later_sources_quota(
    client, db, monkeypatch
):
    _gate(monkeypatch)
    arxiv_results = [
        _arxiv(i, f"{RELEVANT} {i}", "Abstract.", doi=f"10.1/a{i}") for i in range(10)
    ]
    copies = [_s2(f"{RELEVANT} {i}", f"10.1/a{i}") for i in range(5)]
    new = [_s2(f"{RELEVANT} new {i}", f"10.1/s{i}") for i in range(12)]
    _sources(monkeypatch, arxiv_results=arxiv_results, s2_results=copies + new)

    body = client.post("/collect", json={"topic": TOPIC}).json()

    assert db.query(Paper).count() == 20
    s2 = body["source_stats"]["semantic_scholar"]
    assert s2["added"] == 10
    assert s2["cross_source_duplicates"] == 5
    assert s2["capped"] == 2
    merged = [
        d
        for d in _decisions(db, source="semantic_scholar")
        if "cross_source_duplicate_of" in d.evidence
    ]
    assert len(merged) == 5
    assert all(d.decision == "admitted" and d.paper_id for d in merged)
    assert body["duplicates"]["cross_source"] == 5


def test_a_copy_of_a_paper_this_source_already_kept_merges_after_the_cap_fills(
    client, db, monkeypatch
):
    _gate(monkeypatch)
    full = [
        _arxiv(i, f"{RELEVANT} {i}", "Abstract.", doi=f"10.1/a{i}") for i in range(10)
    ]
    copy = _arxiv(50, f"{RELEVANT} 0 v2", "Abstract.", doi="10.1/a0")
    _sources(monkeypatch, arxiv_results=[*full, copy])

    body = client.post("/collect", json={"topic": TOPIC}).json()

    assert body["source_stats"]["arxiv"]["capped"] == 0
    assert db.query(Paper).count() == 10
    assert _decisions(db, decision="capped") == []


def test_a_paper_stored_by_an_earlier_run_uses_no_slot(client, db, monkeypatch):
    """D-CAP-04 (supersedes D-CAP-03's earlier-run rule)."""
    db.add(
        Paper(
            title="Kept", year=2024, doi="10.1/old", source="arxiv", has_full_text=False
        )
    )
    db.commit()
    _gate(monkeypatch)
    results = [_arxiv(0, f"{RELEVANT} old", "Abstract.", doi="10.1/old")]
    results += [_arxiv(i, f"{RELEVANT} {i}", "Abstract.") for i in range(1, 11)]
    _sources(monkeypatch, arxiv_results=results)

    body = client.post("/collect", json={"topic": TOPIC}).json()

    stats = body["source_stats"]["arxiv"]
    assert stats["capped"] == 0
    assert stats["added"] == 10
    assert stats["slots_used"] == 10
    assert stats["already_in_corpus"] == 1
    assert stats["cross_source_duplicates"] == 0
    assert db.query(Paper).count() == 11


def test_the_cap_of_ten_new_papers_holds_with_existing_matches_mixed_in(
    client, db, monkeypatch
):
    for i in range(5):
        db.add(Paper(title=f"Old {i}", year=2024, doi=f"10.1/old{i}", source="arxiv"))
    db.commit()
    _gate(monkeypatch)
    results = [
        _arxiv(i, f"{RELEVANT} old {i}", "Abstract.", doi=f"10.1/old{i}")
        for i in range(5)
    ]
    results += [_arxiv(100 + i, f"{RELEVANT} new {i}", "Abstract.") for i in range(14)]
    _sources(monkeypatch, arxiv_results=results)

    body = client.post("/collect", json={"topic": TOPIC}).json()

    stats = body["source_stats"]["arxiv"]
    assert stats["added"] == 10
    assert stats["capped"] == 4
    assert stats["already_in_corpus"] == 5
    assert db.query(Paper).count() == 15
    assert body["reconciliation"]["ok"] is True


def test_repeated_collections_never_exceed_the_cap_per_run(client, db, monkeypatch):
    """Each run adds at most 10 new papers per source; a re-run re-finding the
    first run's papers merges them and adds at most another 10."""
    _gate(monkeypatch)
    results = [_arxiv(i, f"{RELEVANT} {i}", "Abstract.") for i in range(25)]
    _sources(monkeypatch, arxiv_results=results)

    first = client.post("/collect", json={"topic": TOPIC}).json()
    second = client.post("/collect", json={"topic": TOPIC}).json()

    assert first["source_stats"]["arxiv"]["added"] == 10
    assert second["source_stats"]["arxiv"]["added"] == 10
    assert second["source_stats"]["arxiv"]["already_in_corpus"] == 10
    assert db.query(Paper).count() == 20


def test_no_pdf_is_fetched_for_a_copy_of_a_paper_that_has_full_text(
    client, db, monkeypatch
):
    _gate(monkeypatch)
    title = f"{RELEVANT} twin"
    _sources(
        monkeypatch,
        arxiv_results=[_arxiv(1, title, "Abstract.")],
        acl_results=[
            {
                "title": title,
                "year": 2025,
                "abstract": "Abstract.",
                "acl_id": "2025.acl-1.1",
                "pdf_url": "https://aclanthology.org/2025.acl-1.1.pdf",
            }
        ],
    )
    monkeypatch.setattr(
        arxiv, "fetch_pdf", lambda c, dest_dir, **kw: f"{dest_dir}/{c['arxiv_id']}.pdf"
    )
    acl_fetches = []
    monkeypatch.setattr(
        acl_anthology,
        "fetch_pdf",
        lambda c, dest_dir, **kw: acl_fetches.append(c) or None,
    )

    client.post("/collect", json={"topic": TOPIC})

    [paper] = db.scalars(select(Paper)).all()
    assert paper.has_full_text and paper.acl_id == "2025.acl-1.1"
    assert acl_fetches == []


# ---------------------------------------------------------------------------
# Which candidates a full cap keeps (S1 audit, 2026-10-10)
# ---------------------------------------------------------------------------


def test_a_title_naming_every_facet_outranks_a_higher_score_under_the_cap(
    client, db, monkeypatch
):
    """Run 1 capped a Tamil/Tanglish prompt-injection paper (score -0.27) behind
    a paper that used "prompt injection" only as a fine-tuning technique."""
    strong_title = "Cross-lingual prompt injection monitorability"
    scores = {f"Prompt injection defense {i}": 6.0 for i in range(10)}
    _gate(monkeypatch, FakeScorer(by_title={**scores, strong_title: -0.27}))
    results = [
        _arxiv(i, f"Prompt injection defense {i}", "Attacks in Hindi and Tamil.")
        for i in range(10)
    ]
    results.append(_arxiv(99, strong_title, "Indirect attacks in Tamil and English."))
    _sources(monkeypatch, arxiv_results=results)

    client.post("/collect", json={"topic": TOPIC})

    stored = {p.title for p in db.scalars(select(Paper))}
    assert strong_title in stored
    [capped] = _decisions(db, decision="capped")
    assert capped.title.startswith("Prompt injection defense")


def test_title_only_candidates_take_only_slots_left_over(client, db, monkeypatch):
    _gate(monkeypatch, FakeScorer(by_title={f"{RELEVANT} bare": 9.0}, default=1.0))
    full = [_arxiv(i, f"{RELEVANT} {i}", "Abstract.") for i in range(10)]
    bare = _arxiv(99, f"{RELEVANT} bare", None)
    _sources(monkeypatch, arxiv_results=[bare, *full])

    client.post("/collect", json={"topic": TOPIC})

    [capped] = _decisions(db, decision="capped")
    assert capped.title == f"{RELEVANT} bare"
    assert capped.tier == "title_only"


def test_a_title_only_candidate_fills_a_free_slot_transparently(
    client, db, monkeypatch
):
    _gate(monkeypatch)
    _sources(monkeypatch, arxiv_results=[_arxiv(99, f"{RELEVANT} bare", None)])

    client.post("/collect", json={"topic": TOPIC})

    [decision] = _decisions(db)
    assert decision.decision == "admitted"
    assert decision.tier == "title_only"
    assert decision.reason_code == "relevant:title_only"


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


# ---------------------------------------------------------------------------
# Phase 1 closeout (2026-10-12): auditability of promotions, caps and text
# ---------------------------------------------------------------------------


def _admitted_by_an_earlier_run(db, **paper):
    """A paper an earlier collection admitted for TOPIC (a v1-era admission)."""
    row = Paper(source="arxiv", has_full_text=False, **paper)
    db.add(row)
    db.flush()
    run = collection_audit.start_collection(
        db, TOPIC, {"gate_version": "relevance-gate-v1"}, {}
    )
    collection_audit.record(
        db,
        run,
        "arxiv",
        {"title": row.title},
        RelevanceDecision(True, "relevant:strong", "admitted by gate v1"),
        paper_id=row.id,
    )
    db.commit()
    return row


REJECTED_COPY = _arxiv(0, JAILBREAK, "Jailbreaks in Hindi and Tamil.", doi="10.1/old")


def test_a_promoted_copy_keeps_the_gate_verdict_and_never_takes_a_slot(
    client, db, monkeypatch
):
    """Run 2: six capped records said only ``duplicate_of_admitted``.

    Under D-CAP-04 a promoted copy always merges onto its paper, so it can no
    longer be capped; its record keeps the gate's own verdict and the
    promotion as history (the promote-then-cap sequence itself is pinned at
    unit level, `TestDecisionHistory`).
    """
    old = _admitted_by_an_earlier_run(
        db, title="Old v1 admission", year=2025, doi="10.1/old"
    )
    _gate(monkeypatch)
    full = [_arxiv(i, f"{RELEVANT} {i}", "Abstract.") for i in range(1, 11)]
    _sources(monkeypatch, arxiv_results=[REJECTED_COPY, *full])

    body = client.post("/collect", json={"topic": TOPIC}).json()

    assert _decisions(db, decision="capped") == []
    [promoted] = _decisions(db, reason_code="duplicate_of_admitted")
    assert promoted.decision == "admitted"
    assert promoted.paper_id == old.id
    assert promoted.evidence["gate"]["reason_code"] == "missing_facet:prompt injection"
    assert [h["reason_code"] for h in promoted.evidence["gate_history"]] == [
        "missing_facet:prompt injection",
        "duplicate_of_admitted",
    ]
    stats = body["source_stats"]["arxiv"]
    assert stats["duplicate_promotions"] == 1
    assert stats["capped"] == 0
    assert stats["added"] == 10

    served = client.get(f"/collections/{body['collection_run_id']}/decisions").json()
    [row] = [
        d for d in served["decisions"] if d["reason_code"] == "duplicate_of_admitted"
    ]
    assert row["gate_history_source"] == "recorded"
    assert row["gate_history"][0]["reason_code"] == "missing_facet:prompt injection"
    assert {d["gate_history_source"] for d in served["decisions"]} == {
        "recorded",
        "no_transition",
    }


def test_duplicate_promotions_are_counted_apart_from_new_additions(
    client, db, monkeypatch
):
    old = _admitted_by_an_earlier_run(
        db, title="Old v1 admission", year=2025, doi="10.1/old"
    )
    _gate(monkeypatch)
    _sources(
        monkeypatch,
        arxiv_results=[REJECTED_COPY, _arxiv(1, RELEVANT, "Abstract.")],
    )

    body = client.post("/collect", json={"topic": TOPIC}).json()

    stats = body["source_stats"]["arxiv"]
    assert stats["added"] == 1
    assert stats["duplicate_promotions"] == 1
    assert stats["already_in_corpus"] == 1
    assert body["added"] == 1
    assert body["duplicates"]["already_in_corpus"] == 1
    [promoted] = _decisions(db, reason_code="duplicate_of_admitted")
    assert promoted.paper_id == old.id
    assert "text_audit" not in promoted.evidence
    run = db.get(CollectionRun, body["collection_run_id"])
    assert run.counts["duplicate_promotions"] == 1


def test_a_cross_source_merge_is_never_counted_as_a_new_paper(client, db, monkeypatch):
    _gate(monkeypatch)
    _sources(
        monkeypatch,
        arxiv_results=[_arxiv(1, RELEVANT, "Abstract.", doi="10.1/x")],
        s2_results=[_s2(RELEVANT, "10.1/x")],
    )

    body = client.post("/collect", json={"topic": TOPIC}).json()

    s2 = body["source_stats"]["semantic_scholar"]
    assert s2["added"] == 0
    assert s2["cross_source_duplicates"] == 1
    assert s2["already_in_corpus"] == 0
    assert s2["duplicate_promotions"] == 0
    assert body["added"] == 1
    assert body["duplicates"]["cross_source"] == 1
    assert db.query(Paper).count() == 1


LONG_ABSTRACT = "Jailbreak prompts written in Hindi and Tamil. " * 30


def test_rejected_and_capped_candidates_carry_bounded_text_evidence(
    client, db, monkeypatch
):
    scores = {f"{RELEVANT} {i}": float(i) for i in range(11)}
    _gate(monkeypatch, FakeScorer(by_title=scores))
    results = [_arxiv(i, f"{RELEVANT} {i}", "Abstract.") for i in range(11)]
    results.append(_arxiv(99, JAILBREAK, LONG_ABSTRACT))
    _sources(monkeypatch, arxiv_results=results)

    client.post("/collect", json={"topic": TOPIC})

    [rejected] = _decisions(db, decision="rejected")
    audit = rejected.evidence["text_audit"]
    assert len(audit["abstract_excerpt"]) == 400
    assert audit["excerpt_truncated"] is True
    assert len(audit["sha256"]) == 64
    assert rejected.evidence["facets"]
    [capped] = _decisions(db, decision="capped")
    assert capped.evidence["text_audit"]["abstract_excerpt"] == "Abstract."
    for admitted in _decisions(db, decision="admitted"):
        assert "text_audit" not in admitted.evidence


def test_an_excerpt_bound_of_zero_stores_no_abstract_text(client, db, monkeypatch):
    monkeypatch.setenv("RELEVANCE_AUDIT_EXCERPT_CHARS", "0")
    _gate(monkeypatch)
    _sources(monkeypatch, arxiv_results=[_arxiv(99, JAILBREAK, LONG_ABSTRACT)])

    body = client.post("/collect", json={"topic": TOPIC}).json()

    [rejected] = _decisions(db)
    assert rejected.evidence["text_audit"]["abstract_excerpt"] is None
    assert rejected.evidence["text_audit"]["normalized_length"] > 0
    run = db.get(CollectionRun, body["collection_run_id"])
    assert run.settings["relevance_audit_excerpt_chars"] == 0


def test_a_rejected_candidate_without_an_abstract_says_so(client, db, monkeypatch):
    _gate(monkeypatch)
    _sources(monkeypatch, arxiv_results=[_arxiv(1, JAILBREAK, None)])

    client.post("/collect", json={"topic": TOPIC})

    [rejected] = _decisions(db)
    assert rejected.evidence["text_audit"]["abstract_available"] is False
    assert rejected.evidence["text_audit"]["abstract_excerpt"] is None


def test_full_text_coverage_counts_unique_papers_not_candidates(
    client, db, monkeypatch
):
    _gate(monkeypatch)
    _sources(
        monkeypatch,
        arxiv_results=[
            _arxiv(1, f"{RELEVANT} 1", "Abstract.", doi="10.1/a1"),
            _arxiv(2, f"{RELEVANT} 2", "Abstract."),
        ],
        s2_results=[
            _s2(f"{RELEVANT} 1", "10.1/a1"),  # a copy: one paper, not two
            _s2(f"{RELEVANT} 3", "10.1/s3"),
        ],
    )
    monkeypatch.setattr(
        arxiv,
        "fetch_pdf",
        lambda c, dest_dir, **kw: (
            f"{dest_dir}/1.pdf" if c["arxiv_id"].endswith("1") else None
        ),
    )

    body = client.post("/collect", json={"topic": TOPIC}).json()

    assert body["full_text"]["touched"] == {
        "papers": 3,
        "with_full_text": 1,
        "metadata_only": 2,
        "metadata_only_without_doi": 1,
        "coverage": 0.333,
    }
    assert body["full_text"]["added"]["papers"] == 3
    assert body["full_text"]["scope"]
    run = db.get(CollectionRun, body["collection_run_id"])
    assert run.counts["full_text"] == body["full_text"]


def test_unpaywall_outcomes_are_reported_without_any_network_call(
    client, db, monkeypatch
):
    _gate(monkeypatch)
    _sources(
        monkeypatch,
        s2_results=[_s2(f"{RELEVANT} {i}", f"10.1/s{i}") for i in range(3)],
    )
    lookups = {
        "10.1/s0": OaLookup("no_oa_copy", http_status=200),
        "10.1/s1": OaLookup("timeout"),
        "10.1/s2": OaLookup("oa_location_found", "https://example.org/s2.pdf", 200),
    }
    monkeypatch.setattr(unpaywall, "lookup_oa_pdf", lambda doi, email: lookups[doi])
    monkeypatch.setattr(
        fulltext,
        "safe_download",
        lambda url, dest_path, **kw: DownloadResult("pdf_stored", 200),
    )

    body = client.post("/collect", json={"topic": TOPIC}).json()

    assert body["unpaywall"]["completed"] is True
    assert body["unpaywall"]["outcomes"] == {
        "no_oa_location": 1,
        "lookup_timeout": 1,
        "pdf_stored": 1,
    }
    assert body["unpaywall"]["looked_up"] == 3
    assert body["full_text"]["touched"]["with_full_text"] == 1
    run = db.get(CollectionRun, body["collection_run_id"])
    assert run.counts["unpaywall"] == body["unpaywall"]
    # Every outcome is tied to its paper, over the API.
    attempts = client.get(f"/collections/{body['collection_run_id']}/fulltext").json()
    by_doi = {
        db.get(Paper, a["paper_id"]).doi: a["outcome"] for a in attempts["attempts"]
    }
    assert by_doi == {
        "10.1/s0": "no_oa_location",
        "10.1/s1": "lookup_timeout",
        "10.1/s2": "pdf_stored",
    }


def test_a_failed_fulltext_step_reports_no_outcomes(client, db, monkeypatch):
    """A step that fails is rolled back with every attempt it recorded."""
    _gate(monkeypatch)
    _sources(monkeypatch, s2_results=[_s2(RELEVANT, "10.1/s0")])

    def _down(session, log, **kw):
        paper = db.scalars(select(Paper)).first()
        log.record(paper.id, "unpaywall", "enrichment", "no_oa_location")
        raise RuntimeError("unpaywall down")

    monkeypatch.setattr(routes_ingestion.fulltext, "acquire_missing_full_text", _down)

    body = client.post("/collect", json={"topic": TOPIC}).json()

    assert body["unpaywall"]["completed"] is False
    assert body["unpaywall"]["looked_up"] == 0
    assert body["unpaywall"]["outcomes"] == {}
    assert body["fulltext"]["attempts"] == 0
    assert body["sources"]["fulltext"].startswith("failed")
    assert body["status"] == "partial"


def test_paper_language_is_enforced_and_unverified_admissions_are_counted(
    client, db, monkeypatch
):
    _gate(monkeypatch)
    _sources(
        monkeypatch,
        arxiv_results=[
            _arxiv(1, "Prompt injection in Tamil", ENGLISH_ABOUT_TAMIL),
            _arxiv(2, f"{RELEVANT} bare", None),
            _arxiv(3, f"{RELEVANT} in Tamil script", TAMIL_SCRIPT),
        ],
    )

    body = client.post("/collect", json={"topic": TOPIC}).json()

    assert body["rejected"]["by_reason"] == {"paper_language:non_latin_script": 1}
    assert body["source_stats"]["arxiv"]["language_unverified"] == 1
    assert body["paper_language"]["admitted"] == {"english": 1, "unverified": 1}
    stored = {p.title for p in db.scalars(select(Paper))}
    assert f"{RELEVANT} in Tamil script" not in stored
