"""A draft's rejected claims are withheld, not the whole draft (owner brief, 2026-10-18).

Every claim used to be all-or-nothing: one vetoed claim threw away the
supported ones beside it, and the strict regeneration that followed needs two
more model calls, which the deployed pacing (60 s per call against a 180 s
deadline) cannot fit. The workflow now withholds the claims that fail -- a
deterministic veto, or the LLM verifier naming them -- and returns the rest,
with the number withheld, when:

* at least one claim is left and no more than half were withheld (a draft that
  is mostly wrong is regenerated, not trimmed);
* every citation id in the draft exists (an invented id still rejects it all);
* the verifier's verdict is well-formed and names the claims it rejects.

Each claim that is shown has passed its own vetoes and the verifier, which now
reads exactly the text the user will see. The LLM is a scripted fake: this is
control flow, not live model behaviour.
"""

import pytest

from app.interactive.workflow import run_query
from tests.interactive.test_workflow import FakeRetriever, ScriptedLLM, _services

A = "Existing systems fail outside English."
B = "Detection accuracy drops on code-mixed prompts."
C = "The benchmark contains 300 adversarial prompts."
EVIDENCE = [
    {"id": "a", "paper_id": 1, "text": A, "score": 0.95, "title": "Paper One"},
    {"id": "b", "paper_id": 2, "text": B, "score": 0.95, "title": "Paper Two"},
    {"id": "c", "paper_id": 3, "text": C, "score": 0.95, "title": "Paper Three"},
]
OK = {"grounded": True, "unsupported_claim_indexes": []}
# A claim no cited passage supports: its number and absolute fail the vetoes.
OVERREACH = "Only one system has ever been tested on 9000 prompts."


def _claim(text, *ids):
    return {"text": text, "evidence_ids": list(ids)}


def _draft(*claims, answer="Draft prose the user never sees."):
    return {"answer": answer, "claims": list(claims)}


def _run(llm, question="What do the papers report about multilingual prompts?"):
    return run_query(
        question, _services(FakeRetriever([EVIDENCE]), llm), 3, 12, 2, 2, 0.2, 1
    )


def test_a_vetoed_claim_is_withheld_and_the_supported_ones_are_returned():
    llm = ScriptedLLM(
        answer=[_draft(_claim(A, "a"), _claim(B, "b"), _claim(OVERREACH, "a"))],
        verify=[OK],
    )
    result = _run(llm)
    assert result["outcome"] == "grounded_answer"
    assert [c["text"] for c in result["claims"]] == [A, B]
    assert result["removed_claim_count"] == 1
    assert result["answer"] == f"{A} {B}"
    # No regeneration was needed, so no extra model calls were spent.
    assert result["regeneration_count"] == 0 and len(llm.prompts["answer"]) == 1
    grounding = next(t for t in result["trace"] if t.startswith("grounding:"))
    assert "reason=ok_partial" in grounding and "kept=2" in grounding


def test_the_verifier_checks_only_the_surviving_claims_as_the_user_will_see_them():
    llm = ScriptedLLM(
        answer=[_draft(_claim(A, "a"), _claim(B, "b"), _claim(OVERREACH, "a"))],
        verify=[OK],
    )
    _run(llm)
    prompt = llm.prompts["verify"][0]
    answer_part = prompt.split("ANSWER_TO_VERIFY:", 1)[1].split("CLAIMS_TO_VERIFY", 1)[
        0
    ]
    assert A in answer_part and B in answer_part
    assert "Draft prose the user never sees" not in prompt
    assert "9000" not in prompt


def test_a_withheld_claims_citations_are_not_returned():
    llm = ScriptedLLM(
        answer=[_draft(_claim(A, "a"), _claim(B, "b"), _claim(OVERREACH, "c"))],
        verify=[OK],
    )
    result = _run(llm)
    assert sorted(c["id"] for c in result["citations"]) == ["a", "b"]
    cited = {i for claim in result["claims"] for i in claim["evidence_ids"]}
    assert cited == {c["id"] for c in result["citations"]}


def test_a_citation_shared_with_a_kept_claim_stays_once():
    llm = ScriptedLLM(
        answer=[_draft(_claim(A, "a"), _claim(B, "b"), _claim(OVERREACH, "a"))],
        verify=[OK],
    )
    result = _run(llm)
    assert [c["id"] for c in result["citations"]].count("a") == 1


def test_claims_the_verifier_names_are_withheld_and_the_rest_kept():
    llm = ScriptedLLM(
        answer=[_draft(_claim(A, "a"), _claim(B, "b"))],
        verify=[{"grounded": False, "unsupported_claim_indexes": [1]}],
    )
    result = _run(llm)
    assert result["outcome"] == "grounded_answer"
    assert [c["text"] for c in result["claims"]] == [A]
    assert [c["id"] for c in result["citations"]] == ["a"]
    assert result["removed_claim_count"] == 1


def test_the_verifiers_indexes_refer_to_the_claims_it_was_shown():
    # Claim 0 is vetoed, so the verifier sees [B, C]; its index 1 is C.
    llm = ScriptedLLM(
        answer=[
            _draft(
                _claim(OVERREACH, "a"), _claim(B, "b"), _claim(C, "c"), _claim(A, "a")
            )
        ],
        verify=[{"grounded": False, "unsupported_claim_indexes": [1]}],
    )
    result = _run(llm)
    assert [c["text"] for c in result["claims"]] == [B, A]
    assert result["removed_claim_count"] == 2


def test_a_draft_that_is_mostly_rejected_is_regenerated_not_trimmed():
    llm = ScriptedLLM(
        answer=[
            _draft(
                _claim(A, "a"), _claim(OVERREACH, "b"), _claim(OVERREACH + " X", "c")
            ),
            _draft(_claim(A, "a"), _claim(B, "b")),
        ],
        verify=[OK],
    )
    result = _run(llm)
    assert result["regeneration_count"] == 1 and len(llm.prompts["answer"]) == 2
    assert [c["text"] for c in result["claims"]] == [A, B]
    assert result["removed_claim_count"] == 0


def test_a_draft_with_every_claim_vetoed_still_ends_in_a_refusal():
    llm = ScriptedLLM(answer=[_draft(_claim(OVERREACH, "a"))] * 2)
    result = _run(llm)
    assert result["outcome"] == "grounding_failed"
    assert result["claims"] == [] and result["citations"] == []
    assert result["removed_claim_count"] == 0
    assert "verify" not in llm.prompts


def test_an_invented_citation_still_rejects_the_whole_draft():
    llm = ScriptedLLM(
        answer=[
            _draft(_claim(A, "a"), _claim(B, "b"), _claim(C, "invented")),
            _draft(_claim(A, "a")),
        ],
        verify=[OK],
    )
    result = _run(llm)
    assert result["regeneration_count"] == 1
    assert "reason=invalid_citations" in " ".join(result["trace"])
    assert [c["text"] for c in result["claims"]] == [A]


@pytest.mark.parametrize(
    "verdict",
    [
        {"grounded": False, "unsupported_claim_indexes": []},
        {"grounded": True, "unsupported_claim_indexes": [1]},
        {"grounded": False, "unsupported_claim_indexes": [0, 1]},
        {"grounded": False, "unsupported_claim_indexes": [7]},
    ],
    ids=["rejects_naming_none", "contradictory", "names_all", "names_out_of_range"],
)
def test_a_verdict_that_does_not_name_a_proper_subset_rejects_the_draft(verdict):
    llm = ScriptedLLM(
        answer=[_draft(_claim(A, "a"), _claim(B, "b"))] * 2, verify=[verdict] * 2
    )
    result = _run(llm)
    assert result["outcome"] == "grounding_failed"
    assert result["claims"] == [] and result["citations"] == []
    assert result["removed_claim_count"] == 0


def test_a_complete_answer_reports_nothing_withheld():
    llm = ScriptedLLM(answer=[_draft(_claim(A, "a"), _claim(B, "b"))], verify=[OK])
    result = _run(llm)
    assert result["outcome"] == "grounded_answer" and result["removed_claim_count"] == 0
    assert "reason=ok;" in next(
        t for t in result["trace"] if t.startswith("grounding:")
    )


def test_the_trace_records_counts_and_codes_never_withheld_text():
    llm = ScriptedLLM(
        answer=[_draft(_claim(A, "a"), _claim(B, "b"), _claim(OVERREACH, "a"))],
        verify=[OK],
    )
    result = _run(llm)
    assert not any("9000" in t or "Only one" in t for t in result["trace"])
