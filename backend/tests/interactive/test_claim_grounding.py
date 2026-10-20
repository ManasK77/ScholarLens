"""Claim-to-passage grounding: the 2026-10-13 live overreach as a regression.

The fixture is the live run's own draft (answer + claims) and the four cited
passages exactly as stored in Qdrant. Retrieval and the LLM are scripted
fakes, so these tests establish workflow behaviour, not live model behaviour.
The deterministic checks can only veto; none of them proves support.
"""

import json
from pathlib import Path

import pytest

from app.interactive.claim_check import (
    answer_from_claims,
    check_claim,
    uncovered_sentences,
)
from app.interactive.workflow import run_query
from tests.interactive.test_workflow import FakeRetriever, ScriptedLLM, _services

LIVE = json.loads(
    (
        Path(__file__).resolve().parent.parent / "fixtures" / "mode_b_live_run_2.json"
    ).read_text(encoding="utf-8")
)
PASSAGE = {e["id"][:8]: e["text"] for e in LIVE["evidence"]}
MIPIAD, REVIEWS, ENGLISH_LIT, CRIMINAL = (
    PASSAGE["dcac6746"],  # paper 2: MIPIAD validated on English and Bangla
    PASSAGE["dbf268c7"],  # paper 1: single conference / single LLM limitations
    PASSAGE["04c841dc"],  # paper 2: "Most existing literature ... English-heavy"
    PASSAGE["5e5af377"],  # paper 3: "most publicly documented evaluations ... English"
)
OK = {"grounded": True, "unsupported_claim_indexes": []}
REJECT = {"grounded": False, "unsupported_claim_indexes": [0]}


def _run(llm):
    evidence_rows = [{**e, "score": 0.95} for e in LIVE["evidence"]]
    return run_query(
        LIVE["question"],
        _services(FakeRetriever([evidence_rows]), llm),
        3,
        12,
        2,
        2,
        0.2,
        1,
    )


def _claim(text, *ids):
    return {"text": text, "evidence_ids": list(ids)}


def _draft(answer, *claims):
    return {"answer": answer, "claims": list(claims)}


M_ID, R_ID, E_ID, C_ID = (
    e["id"]
    for e in LIVE["evidence"][:1]
    + LIVE["evidence"][1:2]
    + LIVE["evidence"][2:3]
    + LIVE["evidence"][3:4]
)


# 1, 7 -- supported claims, including a paraphrase, are not vetoed -------------


def test_a_claim_its_cited_passage_states_is_not_vetoed():
    assert check_claim(LIVE["draft"]["claims"][0]["text"], [MIPIAD]) == []


def test_a_supported_paraphrase_is_not_vetoed():
    assert (
        check_claim("MIPIAD's evaluation covered English and Bangla.", [MIPIAD]) == []
    )
    assert (
        check_claim(
            "The review study used one conference and one open-weight LLM.", [REVIEWS]
        )
        == []
    )


# 2-5 -- the recorded overreach patterns ---------------------------------------------


def test_an_exclusivity_claim_one_example_does_not_establish_is_vetoed():
    claim = (
        "MIPIAD is the only multilingual defense evaluated on more than one language."
    )
    assert "unsupported_absolute" in check_claim(claim, [MIPIAD])


def test_a_low_resource_assertion_the_passages_never_make_is_vetoed():
    live_claim = LIVE["draft"]["claims"][1]
    assert "unsupported_term" in check_claim(
        live_claim["text"], [REVIEWS, CRIMINAL, ENGLISH_LIT]
    )


def test_a_literature_level_observation_is_not_attributed_to_particular_studies():
    claim = "These papers report that their evaluation is restricted to English."
    assert "misattributed" in check_claim(claim, [ENGLISH_LIT, CRIMINAL])
    # Stated as what the literature does, it is not vetoed.
    assert (
        check_claim(
            "Most existing literature assumes an English-heavy setting.", [ENGLISH_LIT]
        )
        == []
    )


def test_a_merely_topical_or_irrelevant_passage_is_not_support():
    assert "low_overlap" in check_claim(
        "Detectors fail on Hindi prompts in chatbots.", [REVIEWS]
    )
    # Mentions MIPIAD, but says nothing about a 40% reduction.
    assert "unsupported_term" in check_claim(
        "MIPIAD reduces false positives by 40%.", [MIPIAD]
    )


def test_hyphen_variants_do_not_hide_a_term():
    assert "unsupported_term" in check_claim(
        "Coverage of low‑resource languages is missing.", [MIPIAD]
    )
    assert (
        check_claim(
            "Most existing literature assumes an English‑heavy setting.", [ENGLISH_LIT]
        )
        == []
    )


# 6, 8, 10, 11 -- the workflow ----------------------------------------------------------


def test_the_live_drafts_overreach_is_withheld_and_its_supported_claim_kept():
    # Since 2026-10-18 a vetoed claim is withheld, not the whole draft: the
    # overreaching claim 1 never reaches the user or the verifier, and the
    # verbatim-supported claim 0 is returned with the count of what was cut.
    live = LIVE["draft"]
    llm = ScriptedLLM(answer=[live, live], verify=[OK, OK])
    result = _run(llm)
    assert result["outcome"] == "grounded_answer"
    assert [c["text"] for c in result["claims"]] == [live["claims"][0]["text"]]
    assert [c["id"] for c in result["citations"]] == [M_ID]
    assert result["removed_claim_count"] == 1
    assert result["regeneration_count"] == 0 and len(llm.prompts["answer"]) == 1
    assert "low-resource" not in llm.prompts["verify"][0]
    assert "reason=ok_partial" in result["trace"][4]
    assert "low-resource" not in result["answer"] and "only" not in result["answer"]


def test_the_live_drafts_overreach_alone_is_refused():
    overreach = _draft("x", LIVE["draft"]["claims"][1])
    llm = ScriptedLLM(answer=[overreach, overreach])
    result = _run(llm)
    assert result["outcome"] == "grounding_failed"
    assert result["claims"] == [] and result["citations"] == []
    assert result["regeneration_count"] == 1 and len(llm.prompts["answer"]) == 2
    # Vetoed before the LLM check: no verification call was spent on it.
    assert "verify" not in llm.prompts
    assert "reason=claim_check_failed" in result["trace"][4]


def test_a_rejected_claim_never_rides_along_with_a_supported_one():
    supported = _claim(LIVE["draft"]["claims"][0]["text"], M_ID)
    overreach = _claim("MIPIAD is the only multilingual defense ever tested.", M_ID)
    llm = ScriptedLLM(
        answer=[_draft("x", supported, overreach), _draft("y", supported)], verify=[OK]
    )
    result = _run(llm)
    assert result["outcome"] == "grounded_answer"
    assert [c["text"] for c in result["claims"]] == [supported["text"]]
    assert "only" not in result["answer"]


def test_answer_text_outside_the_claims_never_reaches_the_user():
    supported = _claim(LIVE["draft"]["claims"][0]["text"], M_ID)
    prose = (
        "The only multilingual prompt-injection defense ever evaluated is MIPIAD. "
        + supported["text"]
    )
    llm = ScriptedLLM(answer=[_draft(prose, supported)], verify=[OK])
    result = _run(llm)
    assert result["outcome"] == "grounded_answer"
    assert result["answer"] == supported["text"]
    assert "only" not in result["answer"]
    assert "prose_uncovered=1" in result["trace"][-1]


def test_the_strict_regeneration_is_told_why_each_claim_was_vetoed():
    live = LIVE["draft"]
    supported = _draft("x", _claim(live["claims"][0]["text"], M_ID))
    llm = ScriptedLLM(answer=[_draft("x", live["claims"][1]), supported], verify=[OK])
    result = _run(llm)
    feedback = llm.prompts["answer"][1].split("PREVIOUS_DRAFT_PROBLEMS", 1)[1]
    assert "unsupported_term" in feedback and "misattributed" in feedback
    assert result["outcome"] == "grounded_answer"


# 9 -- passing the deterministic checks is never enough on its own -------------------


@pytest.mark.parametrize("verdict", [REJECT, {}, {"grounded": "maybe"}])
def test_clean_claims_still_need_a_passing_llm_verdict(verdict):
    supported = _draft("x", _claim(LIVE["draft"]["claims"][0]["text"], M_ID))
    llm = ScriptedLLM(answer=[supported, supported], verify=[verdict, verdict])
    result = _run(llm)
    assert result["outcome"] == "grounding_failed"
    assert result["claims"] == [] and result["citations"] == []


def test_a_claim_is_checked_against_its_own_citations_not_the_whole_pool():
    # The Bangla passage is in the pool but cited by a different claim.
    claims = [
        _claim("The review study used one conference and one open-weight LLM.", R_ID),
        _claim("MIPIAD was validated on Bangla.", R_ID),
    ]
    llm = ScriptedLLM(answer=[_draft("x", *claims)] * 2, verify=[OK, OK])
    result = _run(llm)
    # The Bangla claim is withheld; only the claim its passage states remains.
    assert [c["text"] for c in result["claims"]] == [claims[0]["text"]]
    assert result["removed_claim_count"] == 1 and "Bangla" not in result["answer"]


# helpers ----------------------------------------------------------------------------------


def test_the_composed_answer_is_exactly_the_claims():
    claims = [_claim("A is true", "e1"), _claim("B holds.", "e2")]
    assert answer_from_claims(claims) == "A is true. B holds."
    assert uncovered_sentences("A is true. Something else entirely.", claims) == 1


def test_the_trace_names_each_claims_veto_codes_and_never_its_text():
    supported = _claim(LIVE["draft"]["claims"][0]["text"], M_ID)
    overreach = _claim("MIPIAD is the only multilingual defense ever tested.", M_ID)
    llm = ScriptedLLM(answer=[_draft("x", supported, overreach)] * 2, verify=[OK])
    result = _run(llm)
    grounding = [t for t in result["trace"] if t.startswith("grounding:")]
    assert grounding
    assert all("veto_codes=-,unsupported_absolute" in g for g in grounding)
    assert all("MIPIAD" not in t and "only" not in t for t in result["trace"])


def test_the_strict_regeneration_is_told_what_each_veto_means_and_how_to_fix_it():
    live = LIVE["draft"]
    supported = _draft("x", _claim(live["claims"][0]["text"], M_ID))
    llm = ScriptedLLM(answer=[_draft("x", live["claims"][1]), supported], verify=[OK])
    _run(llm)
    feedback = llm.prompts["answer"][1].split("PREVIOUS_DRAFT_PROBLEMS", 1)[1]
    assert "unsupported_term: " in feedback and "its own cited passages" in feedback
    assert "misattributed: " in feedback
    # Only the codes that fired are explained.
    assert "polarity_mismatch: " not in feedback
