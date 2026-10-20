"""A part reported as the whole: the live LV-Q1 "3,630 versus 7,892" failure.

LV-Q1 asked how many languages and Q&A data points the ChatGPT multilingual
fuzzing study (paper 10) covered. The grounded answer said the study
"generated 3,630 question-answer pairs". The cited Table 2 passage gives 3,630
as the RQ1 row and states "Total 7,892"; the study's conclusion also says it
"analyzed a total of 7,892 Q&A data points". Every check passed, because
3,630 is in the cited passage. The claim dropped the scope of its figure.

`fixtures/mode_b_study_results.json` holds the exact passages (checked against
the frozen index export). `partial_total` fires when a claim about the whole
study states a figure that its cited passages show to be one component of a
labelled total (the components sum to it), without scoping the figure or
stating the total. The LLM is a scripted fake: this is control flow, not live
model behaviour.
"""

import json
from pathlib import Path

import pytest

from app.interactive.claim_check import check_claim
from app.interactive.workflow import run_query
from tests.interactive.test_workflow import FakeRetriever, ScriptedLLM, _services

CASE = json.loads(
    (
        Path(__file__).resolve().parent.parent
        / "fixtures"
        / "mode_b_study_results.json"
    ).read_text(encoding="utf-8")
)
BY_PREFIX = {e["id"][:8]: e for e in CASE["evidence"]}
TABLE = BY_PREFIX["b9d4f2c2"]
APPROACH = BY_PREFIX["6152189e"]
CONCLUSION = BY_PREFIX["9a9da019"]
RETRIEVED = [{**e, "score": 0.95} for e in CASE["evidence"]]
# Where both prompts end their instructions and begin the question.
QUESTION_MARK = "\n\nQUESTION:\n"
# The exact claim the live model produced (with its U+2011 hyphen).
LIVE_CLAIM = "The study generated 3,630 question‑answer pairs."
LANGUAGES = "The study covered 121 target languages."
OK = {"grounded": True, "unsupported_claim_indexes": []}


def _veto(claim, *passages):
    return check_claim(
        claim,
        [p["text"] for p in passages],
        [p["title"] for p in passages],
        [p["paper_id"] for p in passages],
        [p["source_type"] for p in passages],
    )


def test_the_live_claim_is_vetoed_as_a_part_reported_as_the_whole():
    assert "partial_total" in _veto(LIVE_CLAIM, APPROACH, TABLE)


def test_the_veto_needs_the_total_in_the_cited_passages():
    # Without Table 2 nothing cited shows 3,630 is a component of anything.
    assert "partial_total" not in _veto(LIVE_CLAIM, APPROACH)


@pytest.mark.parametrize(
    "claim",
    [
        # Scoped to the research question that produced the figure.
        "In RQ1 the study sent 3,630 questions to ChatGPT.",
        # Gives the total the figure is part of.
        "The study generated 3,630 question‑answer pairs in RQ1 and 7,892 in total.",
        "RQ1 used 3,630 of the study's 7,892 Q&A data points.",
        # States the whole.
        "The study analyzed a total of 7,892 Q&A data points.",
        # Figures that are not components of the total.
        LANGUAGES,
        "The study translated 30 malicious questions into 121 languages.",
    ],
)
def test_scoped_totals_and_non_components_are_not_vetoed(claim):
    assert "partial_total" not in _veto(claim, APPROACH, TABLE, CONCLUSION)


def test_a_figure_that_is_not_a_component_of_the_total_is_not_vetoed():
    passage = {
        **TABLE,
        "text": "Table 1: RQ Q&As. RQ1 3,630 RQ2 1,681 Total 9,999 across all runs.",
    }

    assert "partial_total" not in _veto(LIVE_CLAIM, passage)


def test_a_claim_not_about_the_whole_study_is_not_vetoed():
    claim = "ChatGPT refused 1,681 of the questions in RQ1."

    assert "partial_total" not in _veto(claim, APPROACH, TABLE)
    # Not totalizing, no scope word: the claim reports a count, not a study total.
    assert "partial_total" not in _veto("1,681 questions were labeled FALSE.", TABLE)


def _run(llm):
    return run_query(
        CASE["question"],
        _services(FakeRetriever([RETRIEVED]), llm),
        3,
        12,
        2,
        1,
        0.2,
        1,
        min_papers_paper_specific=1,
    )


def _claim(text, *prefixes):
    return {"text": text, "evidence_ids": [BY_PREFIX[p]["id"] for p in prefixes]}


def test_the_part_as_whole_claim_is_withheld_not_shown_in_a_grounded_answer():
    llm = ScriptedLLM(
        answer=[
            {
                "answer": "x",
                "claims": [
                    _claim(LANGUAGES, "6152189e"),
                    _claim(LIVE_CLAIM, "6152189e", "b9d4f2c2"),
                ],
            }
        ],
        verify=[OK],
    )

    result = _run(llm)

    assert result["outcome"] == "grounded_answer"
    assert [c["text"] for c in result["claims"]] == [LANGUAGES]
    assert result["removed_claim_count"] == 1
    assert "3,630" not in result["answer"]
    grounding = next(t for t in result["trace"] if t.startswith("grounding:"))
    assert "partial_total" in grounding


def test_the_scoped_claim_is_shown_with_its_total():
    scoped = (
        "The study analyzed a total of 7,892 Q&A data points, of which RQ1 "
        "accounts for 3,630."
    )
    llm = ScriptedLLM(
        answer=[
            {
                "answer": "x",
                "claims": [
                    _claim(LANGUAGES, "6152189e"),
                    _claim(scoped, "9a9da019", "b9d4f2c2"),
                ],
            }
        ],
        verify=[OK],
    )

    result = _run(llm)

    assert result["outcome"] == "grounded_answer"
    assert result["removed_claim_count"] == 0
    assert "7,892" in result["answer"]


def test_the_answer_prompt_asks_for_the_scope_of_every_figure():
    llm = ScriptedLLM(
        answer=[{"answer": "x", "claims": [_claim(LANGUAGES, "6152189e")]}],
        verify=[OK],
    )

    _run(llm)

    instructions = llm.prompts["answer"][0].split(QUESTION_MARK, 1)[0]
    assert "subset" in instructions and "denominator" in instructions
    assert "total" in instructions and "sample size" in instructions


def test_the_verifier_is_told_a_part_reported_as_the_whole_is_unsupported():
    llm = ScriptedLLM(
        answer=[{"answer": "x", "claims": [_claim(LANGUAGES, "6152189e")]}],
        verify=[OK],
    )

    _run(llm)

    instructions = llm.prompts["verify"][0].split(QUESTION_MARK, 1)[0]
    assert "subset" in instructions and "whole" in instructions
