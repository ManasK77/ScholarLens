"""Offline tests for the Mode B live acceptance check. No request is sent."""

import copy
import json
from hashlib import sha256
from types import SimpleNamespace

import pytest

from app.interactive import live_check

PASSAGE = "  The empirical validation covers English and Bangla only.  "
GRAPH_ID = f"graph:2:{sha256(b'x').hexdigest()[:20]}"


def _body(**overrides):
    body = {
        "answer": "MIPIAD was evaluated on English and Bangla only.",
        "claims": [
            {"text": "English and Bangla only.", "evidence_ids": ["p1", GRAPH_ID]}
        ],
        "citations": [
            {
                "id": "p1",
                "paper_id": 2,
                "title": "MIPIAD",
                "text": PASSAGE.strip(),
                "year": 2025,
                "source_type": "limitation",
            },
            {
                "id": GRAPH_ID,
                "paper_id": 2,
                "title": "MIPIAD",
                "text": "Bangla | domain",
                "year": 2025,
                "source_type": "graph",
            },
        ],
        "grounded": True,
        "retrieval_rounds": 1,
        "retry_count": 0,
        "regeneration_count": 0,
        "trace": [
            "retrieve:round=1;vector=12;graph=30;total=42",
            "assess:sufficient=True;relevant=12;papers=6",
            "rerank",
            "generate:strict=False",
            "grounding:structural=True;grounded=True",
        ],
        "provider": "groq",
        "model": "openai/gpt-oss-20b",
    }
    body.update(overrides)
    return body


SOURCES = {
    "points": {"p1": {"paper_id": 2, "text": PASSAGE}},
    "titles": {2: "MIPIAD", 10: "Other"},
    "entities": {2: {"Bangla", "English"}},
}


def test_a_consistent_grounded_answer_passes_every_check():
    body = _body()
    checks = live_check.check_response(200, body, SOURCES)
    assert all(c["ok"] for c in checks.values()), checks
    assert live_check.verdict(live_check.classify(body), checks) == ("PASS", 0)


@pytest.mark.parametrize(
    ("mutate", "failed"),
    [
        (
            lambda b: b["citations"][0].update(text="Altered quote."),
            "citations_match_stored_sources",
        ),
        (
            lambda b: b["citations"][0].update(paper_id=10, title="Other"),
            "citations_match_stored_sources",
        ),
        (
            lambda b: b["citations"][1].update(text="Klingon | domain"),
            "citations_match_stored_sources",
        ),
        (
            lambda b: b["citations"][0].update(title="Wrong title"),
            "citation_titles_match_corpus",
        ),
        (
            lambda b: b["claims"][0]["evidence_ids"].append("invented"),
            "claims_cite_returned_citations",
        ),
        (lambda b: b.update(retrieval_rounds=4, retry_count=3), "loop_bounds"),
    ],
)
def test_each_inconsistency_fails_its_check(mutate, failed):
    body = copy.deepcopy(_body())
    mutate(body)
    checks = live_check.check_response(200, body, SOURCES)
    assert checks[failed]["ok"] is False
    assert live_check.verdict(live_check.classify(body), checks)[0] == "FAIL"


def test_malformed_responses_fail_without_crashing():
    checks = live_check.check_response(200, {"answer": "x"}, SOURCES)
    assert checks["response_schema"]["ok"] is False
    body = _body(citations=[{"id": "p1"}])
    assert (
        live_check.check_response(200, body, SOURCES)["citation_structure"]["ok"]
        is False
    )


def test_a_safe_refusal_is_told_apart_from_an_answer():
    body = _body(
        answer="Insufficient relevant evidence was found after the allowed retrieval rounds.",
        claims=[],
        citations=[],
        grounded=False,
        retrieval_rounds=3,
        retry_count=2,
        trace=[
            "retrieve:round=1",
            "assess:sufficient=False",
            "reformulate",
            "retrieve:round=2",
            "assess:sufficient=False",
            "reformulate",
            "retrieve:round=3",
            "assess:sufficient=False",
            "insufficient",
        ],
    )
    checks = live_check.check_response(200, body, SOURCES)
    assert live_check.classify(body) == "insufficient_evidence"
    assert live_check.verdict("insufficient_evidence", checks) == ("SAFE_NON_ANSWER", 3)


def test_an_ungrounded_answer_that_still_carries_claims_fails():
    body = _body(grounded=False, answer="Something unexplained.")
    checks = live_check.check_response(200, body, SOURCES)
    assert live_check.classify(body) == "ungrounded_unknown"
    assert checks["non_answer_carries_no_claims"]["ok"] is False


def test_trace_summary_reports_retry_rerank_and_regeneration():
    summary = live_check.trace_summary(
        [
            "retrieve:round=1",
            "assess:sufficient=False;relevant=1;papers=1",
            "reformulate",
            "retrieve:round=2",
            "assess:sufficient=True;relevant=4;papers=3",
            "rerank",
            "generate:strict=False",
            "grounding:structural=False;grounded=False",
            "regenerate:1",
            "generate:strict=True",
            "grounding:structural=True;grounded=True",
        ]
    )
    assert summary["retrieval_rounds"] == 2
    assert summary["reformulations"] == 1
    assert summary["reranked"] is True
    assert summary["regenerations"] == 1
    assert summary["generations"] == 2
    assert summary["grounding_checks"][-1] == "structural=True;grounded=True"


def test_sanitized_output_keeps_only_audit_fields():
    clean = live_check.sanitize(
        {**_body(), "headers": {"X-API-Key": "secret"}, "detail": "x"}
    )
    assert "headers" not in clean and "secret" not in str(clean)
    assert set(clean["citations"][0]) == {
        "id",
        "paper_id",
        "title",
        "year",
        "source_type",
        "text_chars",
        "text_start",
    }


def test_dry_run_never_sends_a_query(monkeypatch):
    import httpx

    def no_post(*_a, **_k):
        raise AssertionError("POST /query sent during a dry run")

    monkeypatch.setattr(httpx, "post", no_post)
    monkeypatch.setattr(
        httpx, "get", lambda *_a, **_k: SimpleNamespace(status_code=200)
    )
    monkeypatch.setattr(
        "app.core.config.get_settings", lambda: SimpleNamespace(api_key="k")
    )
    monkeypatch.setattr(
        "app.embeddings.indexer.EvidenceIndex",
        lambda _s: SimpleNamespace(has_evidence=lambda: True),
    )
    assert live_check.main([]) == 4


def test_the_api_outcome_field_is_preferred_when_present():
    body = _body(
        grounded=False, claims=[], citations=[], outcome="grounding_failed", answer="x"
    )
    assert live_check.classify(body) == "grounding_failed"


def test_a_refusal_that_still_lists_rejected_draft_citations_fails():
    body = _body(
        answer="A grounded answer could not be produced from the retrieved evidence.",
        claims=[],
        grounded=False,
        regeneration_count=1,
    )
    checks = live_check.check_response(200, body, SOURCES)
    assert checks["refusal_lists_no_citations"]["ok"] is False
    assert live_check.verdict("grounding_failed", checks)[0] == "FAIL"


def test_an_insufficient_refusal_that_lists_retrieved_passages_fails():
    body = _body(
        answer="Insufficient relevant evidence was found after the allowed retrieval rounds.",
        claims=[],
        grounded=False,
        outcome="insufficient_evidence",
    )
    checks = live_check.check_response(200, body, SOURCES)
    assert checks["refusal_lists_no_citations"]["ok"] is False


def test_trace_summary_reads_grounding_reason_codes_and_invalid_rewrites():
    summary = live_check.trace_summary(
        [
            "retrieve:round=1",
            "assess:sufficient=False",
            "reformulate:invalid",
            "retrieve:round=2",
            "assess:sufficient=True",
            "rerank",
            "generate:strict=False",
            (
                "grounding:structural=True;grounded=False;claims=2;cited=2;"
                "invalid_citations=0;unsupported=1;reason=verifier_rejected"
            ),
        ]
    )
    assert summary["reformulations"] == 1 and summary["invalid_reformulations"] == 1
    assert summary["grounding_reasons"] == ["verifier_rejected"]


def test_llm_calls_are_counted_from_the_trace():
    # 1 reformulation (an invalid one still cost a call), 2 generations, and one
    # verifier call: the vetoed draft skipped its verification.
    summary = live_check.trace_summary(
        [
            "retrieve:round=1",
            "assess:sufficient=False",
            "reformulate:invalid",
            "retrieve:round=2",
            "assess:sufficient=True",
            "rerank",
            "generate:strict=False",
            "grounding:structural=True;grounded=False;reason=claim_check_failed",
            "regenerate:1",
            "generate:strict=True",
            "grounding:structural=True;grounded=True;reason=ok",
        ]
    )
    assert summary["llm_calls"] == 4


def _live_stubs(monkeypatch, sent):
    import httpx

    body = {
        "outcome": "insufficient_evidence",
        "answer": "Insufficient relevant evidence was found.",
        "claims": [],
        "citations": [],
        "grounded": False,
        "retrieval_rounds": 3,
        "retry_count": 2,
        "regeneration_count": 0,
        "trace": [
            "retrieve:round=1",
            "assess:sufficient=False",
            "reformulate",
            "retrieve:round=2",
            "assess:sufficient=False",
            "reformulate",
            "retrieve:round=3",
            "assess:sufficient=False",
            "insufficient",
        ],
        "provider": "groq",
        "model": "m",
    }

    def post(url, json, headers, timeout):
        sent.append(json)
        return SimpleNamespace(status_code=200, json=lambda: body)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(
        httpx, "get", lambda *_a, **_k: SimpleNamespace(status_code=200)
    )
    monkeypatch.setattr(
        "app.core.config.get_settings", lambda: SimpleNamespace(api_key="k")
    )
    monkeypatch.setattr(
        "app.embeddings.indexer.EvidenceIndex",
        lambda _s: SimpleNamespace(has_evidence=lambda: True),
    )
    monkeypatch.setattr(
        live_check,
        "_sources",
        lambda _s, _c: {"points": {}, "titles": {}, "entities": {}},
    )


def test_a_question_override_is_the_one_question_sent(monkeypatch, capsys):
    sent = []
    _live_stubs(monkeypatch, sent)
    code = live_check.main(
        [
            "--confirm-live",
            "--question", "What is the best way to brew espresso at home?",
            "--test-id", "LV-3",
            "--expected-paper-ids", "",
            "--expected-outcome", "insufficient_evidence",
        ]
    )  # fmt: skip
    assert sent == [{"question": "What is the best way to brew espresso at home?"}]
    result = json.loads(capsys.readouterr().out)
    assert result["test_id"] == "LV-3"
    assert result["expected_outcome"] == "insufficient_evidence"
    assert result["outcome_as_expected"] is True
    assert result["question"].startswith("What is the best way")
    assert code == 3


def test_the_dry_run_plan_shows_the_question_that_would_be_sent(monkeypatch, capsys):
    sent = []
    _live_stubs(monkeypatch, sent)
    assert (
        live_check.main(
            ["--question", "Which languages?", "--expected-paper-ids", "2;9"]
        )
        == 4
    )
    plan = json.loads(capsys.readouterr().out)["plan"]
    assert sent == [] and plan["question"] == "Which languages?"
    assert plan["expected_paper_ids"] == [2, 9]


def test_trace_summary_lists_each_checks_veto_codes():
    summary = live_check.trace_summary(
        [
            (
                "grounding:structural=True;grounded=False;reason=claim_check_failed;"
                "vetoed=2;prose_uncovered=0;"
                "veto_codes=low_overlap,unsupported_name+low_overlap"
            ),
            "grounding:structural=False;grounded=False;reason=invalid_citations;veto_codes=",
        ]
    )
    assert summary["veto_codes"] == [
        [["low_overlap"], ["unsupported_name", "low_overlap"]],
        [],
    ]


# --- withheld claims (2026-10-18) -------------------------------------------------


def test_a_partial_answer_counts_its_verification_call():
    summary = live_check.trace_summary(
        [
            "generate:strict=False",
            "grounding:structural=True;grounded=True;reason=ok_partial",
        ]
    )
    assert summary["llm_calls"] == 2


def test_a_grounded_answer_listing_a_citation_no_claim_cites_fails():
    body = _body(claims=[{"text": "English and Bangla only.", "evidence_ids": ["p1"]}])
    checks = live_check.check_response(200, body, SOURCES)
    assert checks["citations_cited_by_claims"] == {"ok": False, "detail": [GRAPH_ID]}
    assert live_check.check_response(200, _body(), SOURCES)[
        "citations_cited_by_claims"
    ]["ok"]


@pytest.mark.parametrize(
    ("overrides", "ok"),
    [
        ({"removed_claim_count": 2}, True),
        ({"removed_claim_count": -1}, False),
        ({"removed_claim_count": "2"}, False),
        (
            {
                "outcome": "grounding_failed",
                "grounded": False,
                "claims": [],
                "citations": [],
                "answer": "A grounded answer could not be produced.",
                "removed_claim_count": 1,
            },
            False,
        ),
        ({}, True),  # an older API without the field
    ],
)
def test_the_withheld_claim_count_is_checked(overrides, ok):
    checks = live_check.check_response(200, _body(**overrides), SOURCES)
    assert checks["removed_claim_count_valid"]["ok"] is ok


def test_sanitized_output_keeps_the_withheld_claim_count():
    assert live_check.sanitize(_body(removed_claim_count=3))["removed_claim_count"] == 3


def test_a_verification_whose_answer_was_then_off_question_is_counted():
    summary = live_check.trace_summary(
        [
            "generate:strict=False",
            "grounding:structural=True;grounded=False;reason=off_question;verified=1",
            "regenerate:1",
            "generate:strict=True",
            "grounding:structural=True;grounded=False;reason=off_question;verified=0",
        ]
    )
    assert summary["llm_calls"] == 3
