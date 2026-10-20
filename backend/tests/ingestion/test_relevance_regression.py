"""The relevance gate against the labelled S1 regression set.

`data/s1_relevance_regression.json` holds the 2026-10-10 audit's human labels
for run 1's admitted papers and its rejected ACL candidates; the semantic
scores are the production model's, replayed (`relevance_eval.ReplayScorer`).
The labels are one reviewer's judgments, not ground truth -- these tests pin
the gate's behaviour on them so that a change which moves it is noticed.

Gate v1 baseline on this set: precision (CORE) 0.562, precision (CORE or
PARTIAL) 0.688, CORE recall 1.0, CORE-before-non-CORE ranking 0.762, and the
title-only item admitted in the strong tier.
"""

import pytest

from tests.ingestion import relevance_eval


@pytest.fixture(scope="module")
def report():
    dataset = relevance_eval.load_dataset()
    return relevance_eval.evaluate(relevance_eval.build_gate(dataset), dataset)


def test_the_dataset_keeps_every_label_class_distinct():
    labels = {item["label"] for item in relevance_eval.load_dataset()["items"]}

    assert labels == {
        "CORE",
        "PARTIAL",
        "ADJACENT",
        "IRRELEVANT",
        "INSUFFICIENT_EVIDENCE",
    }


def test_every_core_paper_is_still_admitted(report):
    assert report["missed_core"] == []
    assert report["recall_core"] == 1.0


def test_no_irrelevant_paper_is_admitted(report):
    assert not [
        row
        for row in report["rows"]
        if row["label"] == "IRRELEVANT" and row["admitted"]
    ]


def test_precision_improves_on_the_v1_baseline(report):
    assert report["precision_strict"] >= 0.85
    assert report["precision_lenient"] >= 0.85


def test_admitted_core_papers_rank_above_admitted_non_core_papers(report):
    assert report["ranking_pairs"] == 1.0


def test_each_audit_failure_mode_is_rejected_for_its_own_reason(report):
    reasons = {
        row["reason_code"]
        for row in report["rows"]
        if row["label"] in ("PARTIAL", "ADJACENT", "IRRELEVANT") and not row["admitted"]
    }

    assert {
        "benign_sense:prompt injection",
        "future_work_only:multilingual",
        "incidental_mention:multilingual",
        "adjacent_topic:jailbreak",
    } <= reasons


def test_insufficient_evidence_is_never_admitted_above_the_title_only_tier(report):
    for _id, admitted, _reason, tier in report["insufficient_evidence"]:
        assert not admitted or tier == "title_only"


def test_every_labelled_abstract_passes_the_paper_language_check(report):
    """Gate v3's language step must not move the labelled set."""
    gate = relevance_eval.build_gate(relevance_eval.load_dataset())
    for item in relevance_eval.load_dataset()["items"]:
        [decision] = gate.evaluate_many([(item["title"], item["abstract"])])
        expected = "english" if item["abstract"] else "unverified"
        assert decision.evidence["language"]["status"] == expected, item["id"]


# ---------------------------------------------------------------------------
# Known borderline admissions (Phase 1 closeout, 2026-10-12)
#
# `data/s1_known_borderline.json` pins admissions the owner's audit flagged
# as weak but for which no rule was written: XRXNet (run 2) is a multilingual
# legal-text classifier that lists prompt injection among the attacks it
# mitigates. It is NOT labelled CORE. These tests document current behaviour,
# so that a gate change which moves it is noticed and judged deliberately.
# ---------------------------------------------------------------------------


BORDERLINE_PATH = relevance_eval.DATASET_PATH.with_name("s1_known_borderline.json")


def test_xrxnet_is_a_known_borderline_conceptual_admission():
    borderline = relevance_eval.load_dataset(BORDERLINE_PATH)
    [item] = [i for i in borderline["items"] if i["id"] == "run2-decision-117"]
    gate = relevance_eval.build_gate(borderline)

    [decision] = gate.evaluate_many([(item["title"], item["abstract"])])

    assert item["status"] == "BORDERLINE_ADMISSION"
    assert "label" not in item  # deliberately unlabelled: not claimed CORE
    assert decision.admitted
    assert decision.reason_code == item["recorded_decision"]["reason_code"]
    assert decision.tier == "conceptual"
    facets = decision.evidence["facets"]
    assert facets["prompt injection"]["matched"] == ["prompt injection"]
    assert facets["multilingual"]["present"]
    assert decision.evidence["language"]["status"] == "english"


# ---------------------------------------------------------------------------
# The six admissions flagged for review (2026-10-12 v3 run): needs_review,
# never labelled. Their gate behaviour is pinned; nothing here claims they are
# relevant or irrelevant, and no rule was written to remove them.
# ---------------------------------------------------------------------------

BORDERLINE = relevance_eval.load_dataset(BORDERLINE_PATH)
NEEDS_REVIEW = [i for i in BORDERLINE["items"] if i["status"] == "needs_review"]


def test_the_six_flagged_admissions_are_present_and_unlabelled():
    titles = [i["title"] for i in NEEDS_REVIEW]

    assert len(NEEDS_REVIEW) == 6
    for fragment in (
        "Sentra-Guard",
        "Secure RAG",
        "ChatGPT Reliability",
        "XRXNet",
        "Edge LLM Serving",
        "Phishing Email Detection",
    ):
        assert any(fragment in title for title in titles), fragment
    assert all("label" not in item for item in BORDERLINE["items"])


@pytest.mark.parametrize("item", NEEDS_REVIEW, ids=[i["id"] for i in NEEDS_REVIEW])
def test_a_flagged_admission_keeps_its_recorded_decision(item):
    gate = relevance_eval.build_gate(BORDERLINE)

    [decision] = gate.evaluate_many([(item["title"], item["abstract"])])

    assert decision.admitted
    assert decision.reason_code == item["recorded_decision"]["reason_code"]
    assert (
        decision.evidence["title_facets"] == item["recorded_decision"]["title_facets"]
    )
    # Explainable: each facet says which rule evidenced it.
    for facet in decision.evidence["facets"].values():
        assert facet["evidenced_by"] in ("surface_form", "language_names")


# ---------------------------------------------------------------------------
# A list of languages is not a relevance shortcut (2026-10-12)
#
# The multilingual facet may be evidenced by two or more language names. Real
# candidates whose facet rests only on such a list, scored by the production
# cross-encoder: off-topic ones fall below the semantic floor; a relevant
# Tamil/Tanglish study would be lost if the rule were tightened.
# ---------------------------------------------------------------------------

LANGUAGE_LISTS = relevance_eval.load_dataset(
    relevance_eval.DATASET_PATH.with_name("s1_language_list_cases.json")
)


@pytest.mark.parametrize(
    "case",
    LANGUAGE_LISTS["items"],
    ids=[c["title"][:30] for c in LANGUAGE_LISTS["items"]],
)
def test_a_language_list_alone_never_decides_relevance(case):
    gate = relevance_eval.build_gate(LANGUAGE_LISTS)

    [decision] = gate.evaluate_many([(case["title"], case["abstract"])])

    multilingual = decision.evidence["facets"]["multilingual"]
    assert multilingual["evidenced_by"] == "language_names"
    assert multilingual["matched"] == []
    assert (
        decision.evidence["facets"]["prompt injection"]["evidenced_by"]
        == "surface_form"
    )
    assert decision.reason_code == case["expected_reason_code"]
    assert "label" not in case


def test_off_topic_language_lists_are_rejected_by_the_semantic_floor():
    gate = relevance_eval.build_gate(LANGUAGE_LISTS)
    rejected = [
        c
        for c in LANGUAGE_LISTS["items"]
        if c["expected_reason_code"] == "low_semantic_relevance"
    ]

    decisions = gate.evaluate_many([(c["title"], c["abstract"]) for c in rejected])

    assert len(rejected) == 3
    assert all(d.score < gate.semantic_floor for d in decisions)
