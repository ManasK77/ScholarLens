"""/collect after the S1 hardening pass (2026-10-12).

* the run is on record from its first moment (`running`) to its end
  (`completed`, `completed_with_warnings`, `partial`, `failed`), with the
  whole result persisted -- auditable without the response or the logs;
* incomplete retrieval, a stale ACL cache and failed steps are warnings,
  never a silently "ok" exhaustive run;
* the numbers, in their different units, reconcile;
* every PDF fetched while admitting a candidate leaves a per-paper record,
  and the Semantic Scholar open-access link is kept and used;
* the audit endpoints stay behind the API key, and a second concurrent write
  is refused.

The relevance gate is the production gate with a fake scorer; sources, PDF
hosts and Unpaywall are faked (see tests/api/conftest.py).
"""

# The fixtures below are imported from test_collect_relevance and requested by
# name, which ruff reads as redefinitions.
# ruff: noqa: F811

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ingestion import fulltext
from app.ingestion.collection_audit import CollectionRun, FulltextAttempt
from app.ingestion.corpus import Paper
from app.ingestion.pdf_store import DownloadResult
from app.ingestion.retrieval import Candidates
from app.ingestion.retrieval import report as retrieval_report
from app.ingestion.sources import acl_anthology, arxiv, semantic_scholar
from app.main import app
from tests.api.test_collect_relevance import (  # noqa: F401 -- fixtures
    RELEVANT,
    TOPIC,
    _arxiv,
    _gate,
    _s2,
    _settings_env,
    _sources,
    client,
    db,
)

# The real fetcher, captured before any test's fixture replaces it.
REAL_ARXIV_FETCH_PDF = arxiv.fetch_pdf


def _incomplete(items, total):
    return Candidates(
        items,
        retrieval_report(
            query="q",
            params={},
            total_reported=total,
            pages_requested=1,
            pages_succeeded=1,
            records_fetched=len(items),
            records_dropped={},
            candidates=len(items),
            stop_reason="ceiling",
        ),
    )


# ---------------------------------------------------------------------------
# Run lifecycle and the persisted summary
# ---------------------------------------------------------------------------


def test_a_finished_run_is_auditable_without_its_response(client, db, monkeypatch):
    _gate(monkeypatch)
    _sources(monkeypatch, arxiv_results=[_arxiv(1, RELEVANT, "Abstract.")])

    body = client.post("/collect", json={"topic": TOPIC}).json()

    run = db.get(CollectionRun, body["collection_run_id"])
    assert run.status == body["status"] == "completed"
    assert run.finished_at is not None
    assert run.summary["added"] == 1
    assert run.summary["source_stats"] == body["source_stats"]
    assert "ids" not in run.summary
    served = client.get(f"/collections/{run.id}").json()
    assert served["status"] == "completed"
    assert served["summary"]["reconciliation"]["ok"] is True
    assert served["summary"]["units"]["added"].startswith("new papers")


def test_a_run_where_every_source_fails_is_recorded_as_failed(client, db, monkeypatch):
    _gate(monkeypatch)
    _sources(monkeypatch)

    def _down(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(arxiv, "search_topic", _down)
    monkeypatch.setattr(semantic_scholar, "search_topic", _down)
    monkeypatch.setattr(acl_anthology, "search_topic", _down)

    response = client.post("/collect", json={"topic": TOPIC})

    assert response.status_code == 502
    [run] = db.scalars(select(CollectionRun)).all()
    assert run.status == "failed"
    assert run.finished_at is not None
    assert {w["code"] for w in run.summary["warnings"]} == {"source_failed"}
    assert run.summary["sources"]["fulltext"].startswith("skipped")
    assert db.scalars(select(Paper)).all() == []


def test_an_internal_error_still_closes_the_run_as_failed(client, db, monkeypatch):
    _gate(monkeypatch)
    _sources(monkeypatch, arxiv_results=[_arxiv(1, RELEVANT, "Abstract.")])

    def _defect(*args, **kwargs):
        raise KeyError("a bug, not an outage")

    monkeypatch.setattr("app.api.routes_ingestion._run_status", _defect)
    error_client = TestClient(
        app, raise_server_exceptions=False, headers=client.headers
    )

    response = error_client.post("/collect", json={"topic": TOPIC})

    assert response.status_code == 500
    [run] = db.scalars(select(CollectionRun)).all()
    assert run.status == "failed"


# ---------------------------------------------------------------------------
# Warnings and status: nothing incomplete reads as exhaustive
# ---------------------------------------------------------------------------


def test_a_retrieval_ceiling_is_a_visible_warning(client, db, monkeypatch):
    _gate(monkeypatch)
    _sources(monkeypatch)
    monkeypatch.setattr(
        semantic_scholar,
        "search_topic",
        lambda topic, api_key=None, **kw: _incomplete([_s2(RELEVANT, "10.1/x")], 16451),
    )

    body = client.post("/collect", json={"topic": TOPIC}).json()

    assert body["status"] == "completed_with_warnings"
    [warning] = body["warnings"]
    assert warning["source"] == "semantic_scholar"
    assert warning["code"] == "retrieval_ceiling"
    assert "1 of 16451" in warning["message"]
    retrieval = body["source_stats"]["semantic_scholar"]["retrieval"]
    assert retrieval["complete"] is False
    assert retrieval["total_reported"] == 16451


def test_a_stale_acl_cache_is_a_visible_warning(client, db, monkeypatch):
    _gate(monkeypatch)
    _sources(monkeypatch)
    stale = {
        "exists": True,
        "stale": True,
        "age_days": 21.3,
        "max_age_days": 7,
        "refresh": "failed",
        "refresh_error": "network_error",
    }
    monkeypatch.setattr(
        acl_anthology, "download_anthology_export", lambda dest_path, **kw: stale
    )

    body = client.post("/collect", json={"topic": TOPIC}).json()

    assert body["acl_cache"] == stale
    assert body["status"] == "completed_with_warnings"
    [warning] = body["warnings"]
    assert warning["code"] == "stale_cache"
    assert "21.3 days" in warning["message"]
    assert "network_error" in warning["message"]


def test_the_acl_cache_policy_is_passed_from_settings(client, db, monkeypatch):
    monkeypatch.setenv("ACL_ANTHOLOGY_MAX_AGE_DAYS", "3")
    monkeypatch.setenv("ACL_ANTHOLOGY_MIN_RECORDS", "1234")
    _gate(monkeypatch)
    _sources(monkeypatch)
    seen = {}
    monkeypatch.setattr(
        acl_anthology,
        "download_anthology_export",
        lambda dest_path, **kw: seen.update(kw) or None,
    )

    client.post("/collect", json={"topic": TOPIC})

    assert seen["max_age_days"] == 3
    assert seen["min_records"] == 1234
    assert seen["url"] == acl_anthology.EXPORT_URL


def test_a_source_that_returns_nothing_is_not_a_failed_source(client, db, monkeypatch):
    """Zero new papers from a source that answered is "ok", with its counts;
    a source that failed says so -- the two are never confused."""
    _gate(monkeypatch)
    _sources(monkeypatch, s2_results=[_s2(RELEVANT, "10.1/x")])

    body = client.post("/collect", json={"topic": TOPIC}).json()

    assert body["sources"]["arxiv"] == "ok"
    assert body["source_stats"]["arxiv"]["candidates"] == 0
    assert body["source_stats"]["arxiv"]["added"] == 0
    assert "source_failed" not in [w["code"] for w in body["warnings"]]


# ---------------------------------------------------------------------------
# Reconciliation across units
# ---------------------------------------------------------------------------


def test_counts_reconcile_across_merges_rejections_and_caps(client, db, monkeypatch):
    _gate(monkeypatch)
    arxiv_results = [
        _arxiv(i, f"{RELEVANT} {i}", "Abstract.", doi=f"10.1/a{i}") for i in range(12)
    ]
    arxiv_results.append(_arxiv(99, "Multilingual Jailbreak Study", "Jailbreaks."))
    s2_results = [_s2(f"{RELEVANT} {i}", f"10.1/a{i}") for i in range(3)]
    s2_results += [_s2(f"{RELEVANT} s2 {i}", f"10.1/s{i}") for i in range(2)]
    _sources(monkeypatch, arxiv_results=arxiv_results, s2_results=s2_results)

    body = client.post("/collect", json={"topic": TOPIC}).json()

    reconciliation = body["reconciliation"]
    assert reconciliation["ok"] is True, reconciliation
    assert reconciliation["decisions"] == 18
    assert reconciliation["merge_events"] == 3
    assert body["added"] == 12  # 10 arXiv (2 capped) + 2 Semantic Scholar


# ---------------------------------------------------------------------------
# Per-paper PDF records for fetches made while admitting
# ---------------------------------------------------------------------------


def test_a_discovery_fetch_is_recorded_against_its_paper(client, db, monkeypatch):
    _gate(monkeypatch)
    _sources(monkeypatch, arxiv_results=[_arxiv(1, RELEVANT, "Abstract.")])
    monkeypatch.setattr(arxiv, "fetch_pdf", REAL_ARXIV_FETCH_PDF)
    monkeypatch.setattr(
        arxiv,
        "download_pdf_result",
        lambda url, dest_path, **kw: DownloadResult(
            "not_pdf", 200, detail="content_type"
        ),
    )

    body = client.post("/collect", json={"topic": TOPIC}).json()

    [paper] = db.scalars(select(Paper)).all()
    attempts = client.get(f"/collections/{body['collection_run_id']}/fulltext").json()
    discovery = [a for a in attempts["attempts"] if a["stage"] == "discovery"]
    assert discovery == [
        {
            **discovery[0],
            "paper_id": paper.id,
            "provider": "arxiv",
            "outcome": "not_pdf",
            "http_status": 200,
            "location": "arxiv.org/pdf/2501.00001",
            "stored": False,
        }
    ]
    # The enrichment step then tried the paper's other routes.
    assert any(a["stage"] == "enrichment" for a in attempts["attempts"])


def test_a_merge_onto_a_paper_with_full_text_records_the_reuse(client, db, monkeypatch):
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

    client.post("/collect", json={"topic": TOPIC})

    rows = db.scalars(select(FulltextAttempt).order_by(FulltextAttempt.id)).all()
    assert [(r.provider, r.stage, r.outcome) for r in rows] == [
        ("arxiv", "discovery", "pdf_stored"),
        ("acl_anthology", "discovery", "existing_full_text_reused"),
    ]


def test_the_semantic_scholar_open_access_link_is_kept_and_used(
    client, db, monkeypatch
):
    _gate(monkeypatch)
    s2 = {
        **_s2(RELEVANT, "10.1/x"),
        "openaccess_pdf_url": "https://repo.example.org/x.pdf",
        "source_url": "https://www.semanticscholar.org/paper/abc",
    }
    _sources(monkeypatch, s2_results=[s2])
    tried = []
    monkeypatch.setattr(
        fulltext,
        "safe_download",
        lambda url, dest_path, **kw: (
            tried.append(url) or DownloadResult("pdf_stored", 200)
        ),
    )

    body = client.post("/collect", json={"topic": TOPIC}).json()

    [paper] = db.scalars(select(Paper)).all()
    assert paper.openaccess_pdf_url == "https://repo.example.org/x.pdf"
    assert paper.source_url == "https://www.semanticscholar.org/paper/abc"
    assert tried == ["https://repo.example.org/x.pdf"]
    assert paper.has_full_text is True
    assert body["fulltext"]["by_provider"] == {"semantic_scholar_oa": {"pdf_stored": 1}}
    assert body["full_text"]["touched"]["with_full_text"] == 1


# ---------------------------------------------------------------------------
# The audit endpoints and concurrent writes
# ---------------------------------------------------------------------------


def test_every_audit_endpoint_requires_the_api_key(client, db, monkeypatch):
    _gate(monkeypatch)
    _sources(monkeypatch, arxiv_results=[_arxiv(1, RELEVANT, "Abstract.")])
    run_id = client.post("/collect", json={"topic": TOPIC}).json()["collection_run_id"]
    anonymous = TestClient(app)
    wrong = TestClient(app, headers={"X-API-Key": "not-the-key"})

    for path in (
        f"/collections/{run_id}",
        f"/collections/{run_id}/decisions",
        f"/collections/{run_id}/fulltext",
    ):
        assert anonymous.get(path).status_code == 401, path
        assert wrong.get(path).status_code == 401, path
        assert client.get(path).status_code == 200, path


def test_an_unknown_run_is_404_on_every_audit_endpoint(client):
    for path in (
        "/collections/999",
        "/collections/999/decisions",
        "/collections/999/fulltext",
    ):
        assert client.get(path).status_code == 404, path


def test_audit_responses_carry_no_secrets_and_bounded_text(client, db, monkeypatch):
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "s2-secret-value")
    _gate(monkeypatch)
    long_abstract = "Jailbreak prompts written in Hindi and Tamil. " * 50
    _sources(
        monkeypatch,
        arxiv_results=[_arxiv(1, "Multilingual Jailbreak Study", long_abstract)],
    )
    run_id = client.post("/collect", json={"topic": TOPIC}).json()["collection_run_id"]

    for path in (f"/collections/{run_id}", f"/collections/{run_id}/decisions"):
        text = client.get(path).text
        assert "s2-secret-value" not in text
        assert "test-api-key-ingestion" not in text
        assert long_abstract.strip() not in text
    decisions = client.get(f"/collections/{run_id}/decisions").json()["decisions"]
    assert len(decisions[0]["evidence"]["text_audit"]["abstract_excerpt"]) == 400


def test_a_second_concurrent_collection_is_refused(client, db, monkeypatch):
    from app.core.pipeline import writer_lock

    _gate(monkeypatch)
    _sources(monkeypatch)

    with writer_lock(db):
        response = client.post("/collect", json={"topic": TOPIC})

    assert response.status_code == 409
    assert db.scalars(select(CollectionRun)).all() == []
