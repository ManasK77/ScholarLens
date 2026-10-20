"""The S1 audit trail's evidence helpers (Phase 1 closeout, 2026-10-12).

* `text_audit` -- bounded evidence for rejected and capped candidates: a hash
  and length of the text the gate judged, and at most a configured number of
  characters of the abstract, so a false negative can be reviewed without
  storing whole abstracts.
* `gate_history` -- every verdict a candidate went through. New records store
  it; older records (gate v1/v2 runs) are reconstructed from what they kept,
  and say so.
"""

import hashlib

from app.ingestion.collection_audit import CandidateDecision, gate_history, text_audit
from app.ingestion.relevance import RelevanceDecision, normalize

TITLE = "Multilingual Jailbreak Study"
ABSTRACT = "Jailbreak prompts written in Hindi and Tamil. " * 20


def test_the_hash_and_length_are_of_the_normalized_text_the_gate_judged():
    audit = text_audit({"title": TITLE, "abstract": ABSTRACT}, 400)

    normalized = normalize(f"{TITLE} {' '.join(ABSTRACT.split())}")
    assert audit["sha256"] == hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    assert audit["normalized_length"] == len(normalized)
    assert audit["normalization"]


def test_the_abstract_excerpt_is_truncated_to_the_configured_bound():
    audit = text_audit({"title": TITLE, "abstract": ABSTRACT}, 400)

    assert len(audit["abstract_excerpt"]) == 400
    assert ABSTRACT.startswith(audit["abstract_excerpt"])
    assert audit["excerpt_truncated"] is True
    assert audit["abstract_length"] == len(" ".join(ABSTRACT.split()))
    assert audit["excerpt_max_chars"] == 400


def test_a_short_abstract_is_kept_whole_and_not_marked_truncated():
    audit = text_audit({"title": TITLE, "abstract": "Short abstract."}, 400)

    assert audit["abstract_excerpt"] == "Short abstract."
    assert audit["excerpt_truncated"] is False


def test_an_excerpt_bound_of_zero_stores_no_excerpt_but_keeps_the_hash():
    audit = text_audit({"title": TITLE, "abstract": ABSTRACT}, 0)

    assert audit["abstract_excerpt"] is None
    assert audit["excerpt_truncated"] is None
    assert len(audit["sha256"]) == 64
    assert audit["normalized_length"] > 0


def test_a_missing_abstract_is_recorded_honestly():
    audit = text_audit({"title": TITLE, "abstract": None}, 400)

    assert audit["abstract_available"] is False
    assert audit["abstract_excerpt"] is None
    assert audit["abstract_length"] == 0
    assert audit["normalized_length"] == len(normalize(TITLE))


# ---------------------------------------------------------------------------
# gate_history: recorded for new rows, reconstructed for older ones
# ---------------------------------------------------------------------------


def _row(decision, reason_code, reason, evidence):
    return CandidateDecision(
        decision=decision, reason_code=reason_code, reason=reason, evidence=evidence
    )


def test_a_recorded_history_is_returned_as_is():
    capped = (
        RelevanceDecision(False, "adjacent_topic:jailbreak", "r")
        .as_duplicate_of(6)
        .capped("arxiv", 10)
    )
    row = _row("capped", capped.reason_code, capped.reason, capped.evidence)

    history, source = gate_history(row)

    assert source == "recorded"
    assert history == capped.evidence["gate_history"]


def test_a_row_with_no_transition_is_its_own_gate_verdict():
    """Not "reconstructed": nothing happened after the gate, so there was no
    history to lose (2026-10-12 audit: 72 of 75 rows were mislabelled)."""
    row = _row("admitted", "relevant:strong", "Evidence.", {"facets": {}})

    history, source = gate_history(row)

    assert source == "no_transition"
    assert history == [
        {
            "step": "gate",
            "admitted": True,
            "reason_code": "relevant:strong",
            "reason": "Evidence.",
        }
    ]


def test_an_older_promoted_duplicate_shows_the_gate_verdict_then_the_promotion():
    row = _row(
        "admitted",
        "duplicate_of_admitted",
        "Same paper as corpus paper 7.",
        {
            "gate": {
                "admitted": False,
                "reason_code": "insufficient_text",
                "reason": "No abstract.",
            }
        },
    )

    history, source = gate_history(row)

    assert source == "reconstructed"
    assert [(h["step"], h["reason_code"]) for h in history] == [
        ("gate", "insufficient_text"),
        ("duplicate_promotion", "duplicate_of_admitted"),
    ]


def test_an_older_capped_promotion_recovers_the_overwritten_verdict_from_its_text():
    """Run 2's records 78/86/89/93/122/126: evidence["gate"] holds the
    promotion; the gate's own verdict survives only in the promotion's
    reason text, and is recovered from it and labelled as such."""
    row = _row(
        "capped",
        "source_cap_reached",
        "Passed the relevance gate (duplicate_of_admitted, score n/a) but ...",
        {
            "gate": {
                "admitted": True,
                "reason_code": "duplicate_of_admitted",
                "reason": "Same paper as corpus paper 6, already admitted for this "
                "topic; the gate's verdict on this copy alone was "
                "'adjacent_topic:jailbreak'.",
            }
        },
    )

    history, source = gate_history(row)

    assert source == "reconstructed"
    assert [(h["step"], h["reason_code"]) for h in history] == [
        ("gate", "adjacent_topic:jailbreak"),
        ("duplicate_promotion", "duplicate_of_admitted"),
        ("source_cap", "source_cap_reached"),
    ]
    assert history[0]["recovered_from"] == "duplicate_promotion_reason"
    assert history[0]["admitted"] is False


def test_an_upload_is_an_owner_decision_not_a_gate_verdict():
    row = _row("admitted", "owner_upload", "Uploaded by the owner.", {})

    history, source = gate_history(row)

    assert [h["step"] for h in history] == ["owner_upload"]
    assert source == "no_transition"


def test_a_rejected_row_with_no_transition_is_not_reconstructed():
    row = _row("rejected", "missing_facet:x", "No evidence.", {"facets": {}})

    history, source = gate_history(row)

    assert source == "no_transition"
    assert history == [
        {
            "step": "gate",
            "admitted": False,
            "reason_code": "missing_facet:x",
            "reason": "No evidence.",
        }
    ]


def test_the_last_history_entry_always_matches_the_stored_decision():
    rows = [
        _row("admitted", "relevant:strong", "r", {}),
        _row("rejected", "missing_facet:x", "r", {}),
        _row(
            "admitted",
            "duplicate_of_admitted",
            "r",
            {
                "gate": {
                    "admitted": False,
                    "reason_code": "insufficient_text",
                    "reason": "",
                }
            },
        ),
    ]
    capped = RelevanceDecision(True, "relevant:strong", "r").capped("arxiv", 10)
    rows.append(_row("capped", capped.reason_code, capped.reason, capped.evidence))

    for row in rows:
        history, _ = gate_history(row)
        assert history[-1]["reason_code"] == row.reason_code
        assert history[-1]["admitted"] is (row.decision == "admitted")


def test_the_hash_verifies_against_the_full_text_not_the_excerpt():
    """The stored excerpt is a prefix; the hash is over the whole normalized
    title + abstract, so it can only be verified from the source text."""
    candidate = {"title": TITLE, "abstract": ABSTRACT}
    audit = text_audit(candidate, 400)

    full = normalize(f"{TITLE} {' '.join(ABSTRACT.split())}")
    from_excerpt = normalize(f"{TITLE} {audit['abstract_excerpt']}")
    assert audit["sha256"] == hashlib.sha256(full.encode("utf-8")).hexdigest()
    assert audit["sha256"] != hashlib.sha256(from_excerpt.encode("utf-8")).hexdigest()


def test_an_empty_candidate_still_gets_a_hash_of_its_empty_text():
    audit = text_audit({"title": None, "abstract": None}, 400)

    assert audit["normalized_length"] == 0
    assert audit["sha256"] == hashlib.sha256(b"").hexdigest()
    assert audit["abstract_available"] is False
