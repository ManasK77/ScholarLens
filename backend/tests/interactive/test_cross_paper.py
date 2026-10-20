"""Cross-paper synthesis on real passages: the live LV-2 refusal as a regression.

LV-2/2b/2c ("Which papers report that LLM safety or instruction compliance is
weaker for non-English prompts?") were refused three times by
`unsupported_term` and `misattributed` vetoes; the drafts were never
recorded, so whether the refusals were right could not be checked.

`fixtures/mode_b_cross_paper.json` holds seven exact passages from the frozen
index (verified against Qdrant), each with the role it was given by reading it
*before* any claim below was written (agent-assigned, provisional):

* paper 3 and paper 10 state the finding as their own result;
* paper 1's "Prior studies have observed ..." reports other work;
* paper 1 (Arabic injection had weaker effects) and paper 5 (worse when the
  prompt is in English) point the other way.

Replaying candidate claims against them found real false rejections of
correct claims: a "Paper 10 reports ..." reference read as the number 10, a
passage sentence joining a positive and a negated clause with ", and" read as
contradicting the positive half, and "none of" not matched by "all ... do not".
These tests pin the fixes and that the genuine faults are still caught. The
LLM is a scripted fake; this is not live model behaviour.
"""

import json
from pathlib import Path

import pytest

from app.interactive.claim_check import check_claim, check_claims, sentences
from app.interactive.workflow import run_query
from tests.interactive.test_workflow import FakeRetriever, ScriptedLLM, _services

CASE = json.loads(
    (
        Path(__file__).resolve().parent.parent / "fixtures" / "mode_b_cross_paper.json"
    ).read_text(encoding="utf-8")
)
BY_PREFIX = {e["id"][:8]: e for e in CASE["evidence"]}
ALL_LANGS, TURKISH, JAILBREAK, RISK, PRIOR, ARABIC, ENGLISH_WORSE = (
    BY_PREFIX[p]
    for p in (
        "b6620f6d", "a61a1ab2", "6f2edf7b", "62047eb2", "51855923", "924957e8",
        "2f15b77e",
    )
)  # fmt: skip
OK = {"grounded": True, "unsupported_claim_indexes": []}


def _veto(text, *items):
    claims = [{"text": text, "evidence_ids": [i["id"] for i in items]}]
    return check_claims(claims, {i["id"]: i for i in CASE["evidence"]})[0]


def _claim(text, *items):
    return {"text": text, "evidence_ids": [i["id"] for i in items]}


def _run(llm):
    rows = [{**e, "score": 0.93} for e in CASE["evidence"]]
    return run_query(
        CASE["question"],
        _services(FakeRetriever([rows]), llm),
        3,
        12,
        2,
        2,
        0.2,
        1,
        min_papers_paper_specific=1,
    )


def test_the_fixture_roles_are_what_the_passages_say():
    assert {
        e["paper_id"] for e in CASE["evidence"] if e["role"].startswith("supports")
    } == {
        3,
        10,
    }
    assert "Prior studies have observed" in PRIOR["text"]
    assert "weaker effects" in ARABIC["text"]
    assert "written in English" in ENGLISH_WORSE["text"]


# --- correct claims about the real passages pass the vetoes ----------------------


@pytest.mark.parametrize(
    ("claim", "item"),
    [
        (
            (
                "All evaluated LLMs showed higher full-compliance rates for "
                "non-English prompts than for English prompts."
            ),
            ALL_LANGS,
        ),
        (
            (
                "Turkish, Simplified Chinese and Russian showed systematically higher "
                "full compliance rates than English."
            ),
            TURKISH,
        ),
        ("Non-English questions might bypass ChatGPT's safety checks.", JAILBREAK),
        (
            (
                "Arabic prompt injection had markedly weaker effects, with limited "
                "score drift and fewer decision changes."
            ),
            ARABIC,
        ),
        (
            "Performance deteriorated severely when the prompt was written in English.",
            ENGLISH_WORSE,
        ),
    ],
)
def test_a_correct_claim_about_its_own_passage_passes(claim, item):
    assert _veto(claim, item) == []


def test_naming_the_cited_paper_by_its_id_is_not_an_unsupported_number():
    assert _veto("Paper 10 reports that non-English questions might pose a risk of "
                 "jailbreak.", RISK) == []  # fmt: skip
    assert _veto("Non-English prompts had higher full-compliance rates in all "
                 "evaluated LLMs (paper 3).", ALL_LANGS) == []  # fmt: skip


def test_attributing_a_finding_to_a_paper_it_did_not_cite_is_wrong_paper():
    vetoes = _veto(
        "Paper 5 reports that all evaluated LLMs had higher full-compliance rates "
        "for non-English prompts.",
        ALL_LANGS,
    )
    assert "wrong_paper" in vetoes


def test_a_positive_clause_joined_to_a_negated_one_is_not_a_polarity_flip():
    claim = (
        "184 questions (5.07%) led to ChatGPT jailbreaks, while their English "
        "versions did not."
    )
    assert _veto(claim, JAILBREAK) == []


def test_restating_only_the_positive_half_of_an_and_joined_sentence_passes():
    # Read as one clause, the passage sentence is "negated" (its second half),
    # so the positive half restated alone looked like a polarity flip.
    claim = "184 questions (5.07%) resulted in ChatGPT jailbreaks."
    assert _veto(claim, JAILBREAK) == []


def test_none_of_is_supported_by_all_do_not():
    claim = (
        "184 questions (5.07%) led to ChatGPT jailbreaks, while none of their English "
        "versions did."
    )
    assert "unsupported_absolute" not in _veto(claim, JAILBREAK)


# --- the genuine faults are still caught ----------------------------------------


def test_the_citing_papers_report_of_prior_work_is_still_misattributed():
    vetoes = _veto(
        "The reviewing study reports that models exhibit uneven compliance in "
        "non-English settings.",
        PRIOR,
    )
    assert "misattributed" in vetoes


def test_the_live_lvq2_prior_work_claim_is_misattributed():
    # Live LV-Q2 (2026-10-18), verbatim, including the model's U+202F: stripping
    # "Paper 1" removed the attribution subject, and "observes" was not an
    # attribution verb, so it was returned as grounded.
    claim = (
        "Paper 1 observes that instruction‑following behavior in multilingual "
        "LLMs varies across languages, with uneven compliance in non‑English "
        "settings, implying weaker instruction compliance for non‑English prompts."
    )
    assert "misattributed" in _veto(claim, PRIOR)
    # The paper-3 claim of the same answer, about paper 3's own result, passes.
    own = (
        "Paper 3 reports that safety alignment is uneven across languages, with "
        "non‑English languages (e.g., Turkish, Simplified Chinese, Russian) "
        "exhibiting systematically higher full compliance rates than English, "
        "indicating weaker safety for non‑English prompts."
    )
    assert _veto(own, TURKISH, ALL_LANGS) == []


@pytest.mark.parametrize("verb", ["observes", "finds", "found", "shows", "notes"])
def test_attribution_verbs_beyond_report_are_read(verb):
    claim = f"The reviewing study {verb} uneven compliance in non-English settings."
    assert "misattributed" in _veto(claim, PRIOR)


def test_a_detail_cited_to_a_passage_that_lacks_it_is_vetoed():
    # True of paper 3, but the cited passage does not name Turkish.
    assert "unsupported_term" in _veto("Turkish showed higher full compliance than "
                                       "English.", ALL_LANGS)  # fmt: skip


def test_reversing_the_passages_direction_is_vetoed():
    assert _veto(
        "Performance deteriorated severely when the prompt was written in a "
        "non-English language.",
        ENGLISH_WORSE,
    )


def test_an_unsupported_consensus_claim_is_vetoed():
    assert "unsupported_absolute" in _veto(
        "All studies find that LLM safety is weaker for non-English prompts.",
        ALL_LANGS,
        JAILBREAK,
    )


@pytest.mark.parametrize(
    ("claim", "item"),
    [
        (
            "No study has evaluated Arabic prompt injection against LLM reviewers.",
            ARABIC,
        ),
        ("Cross-lingual safety of LLMs has not been studied before.", ALL_LANGS),
        ("Multilingual jailbreak defenses remain unexplored.", JAILBREAK),
    ],
)
def test_a_gap_inferred_from_absence_is_vetoed(claim, item):
    assert "unsupported_absence" in _veto(claim, item)


def test_a_gap_a_passage_states_is_not_vetoed_as_absence():
    stated = "Cross-lingual robustness remains underexplored in LLM security."
    assert "unsupported_absence" not in check_claim(stated, [stated])


# --- sentence splitting behind the per-sentence checks ----------------------------


def test_a_sentence_after_a_bracketed_citation_is_split_off():
    text = "Prior work studied English [3].We find that Turkish prompts fail."
    assert sentences(text) == [
        "Prior work studied English [3].",
        "We find that Turkish prompts fail.",
    ]


def test_an_own_finding_after_a_prior_work_sentence_is_not_misattributed():
    passage = (
        "Prior work studied English prompts only [3].Our evaluation covers Turkish "
        "and Russian prompts."
    )
    claim = "The study reports that its evaluation covers Turkish and Russian prompts."
    assert "misattributed" not in check_claim(claim, [passage])


def test_a_sentence_reporting_cited_references_is_literature_level():
    # The second sentence of paper 1's passage, on its own: it reports [13], [16].
    passage = (
        "Since alignment procedures are often English-centric, models may exhibit "
        "uneven compliance in non-English settings [13], [16]."
    )
    claim = "The study reports uneven compliance in non-English settings."
    assert "misattributed" in check_claim(claim, [passage])
    own = "We observe uneven compliance in non-English settings, unlike [13]."
    assert "misattributed" not in check_claim(claim, [own])


# --- end to end: the synthesis is answered, the faults are withheld ----------------


def test_a_correct_two_paper_synthesis_is_a_grounded_answer():
    claims = [
        _claim(
            "All evaluated LLMs showed higher full-compliance rates for non-English "
            "prompts than for English prompts.",
            ALL_LANGS,
        ),
        _claim(
            "Non-English questions might bypass ChatGPT's safety checks.", JAILBREAK
        ),
    ]
    llm = ScriptedLLM(answer=[{"answer": "x", "claims": claims}], verify=[OK])
    result = _run(llm)
    assert result["outcome"] == "grounded_answer" and result["removed_claim_count"] == 0
    assert {c["paper_id"] for c in result["citations"]} == {3, 10}
    # Each claim keeps its own citation; neither borrows the other's paper.
    by_text = {c["text"]: c["evidence_ids"] for c in result["claims"]}
    assert by_text[claims[0]["text"]] == [ALL_LANGS["id"]]
    assert by_text[claims[1]["text"]] == [JAILBREAK["id"]]


def test_the_lv2_shape_keeps_the_supported_claim_and_withholds_the_misattributed_one():
    claims = [
        _claim(
            "The reviewing study reports that models exhibit uneven compliance in "
            "non-English settings.",
            PRIOR,
        ),
        _claim(
            "Turkish, Simplified Chinese and Russian showed systematically higher "
            "full compliance rates than English.",
            TURKISH,
        ),
    ]
    llm = ScriptedLLM(answer=[{"answer": "x", "claims": claims}], verify=[OK])
    result = _run(llm)
    assert result["outcome"] == "grounded_answer"
    assert [c["text"] for c in result["claims"]] == [claims[1]["text"]]
    assert result["removed_claim_count"] == 1
    assert {c["paper_id"] for c in result["citations"]} == {3}


def test_disagreeing_evidence_is_reported_as_each_papers_own_result():
    claims = [
        _claim(
            "All evaluated LLMs showed higher full-compliance rates for non-English "
            "prompts than for English prompts.",
            ALL_LANGS,
        ),
        _claim(
            "Arabic prompt injection had markedly weaker effects, with limited score "
            "drift and fewer decision changes.",
            ARABIC,
        ),
    ]
    llm = ScriptedLLM(answer=[{"answer": "x", "claims": claims}], verify=[OK])
    result = _run(llm)
    assert result["outcome"] == "grounded_answer"
    assert {c["paper_id"] for c in result["citations"]} == {3, 1}


def test_a_synthesis_with_no_supported_claim_is_still_refused():
    claims = [
        _claim(
            "The reviewing study reports that models exhibit uneven compliance in "
            "non-English settings.",
            PRIOR,
        ),
        _claim(
            "Paper 5 reports that safety is weaker for non-English prompts.", ARABIC
        ),
    ]
    llm = ScriptedLLM(answer=[{"answer": "x", "claims": claims}] * 2)
    result = _run(llm)
    assert result["outcome"] == "grounding_failed"
    assert result["claims"] == [] and result["citations"] == []


def test_the_answer_prompt_shows_each_passages_section_and_attribution_rules():
    claim = _claim(
        "Non-English questions might bypass ChatGPT's safety checks.", JAILBREAK
    )
    llm = ScriptedLLM(answer=[{"answer": "x", "claims": [claim]}], verify=[OK])
    _run(llm)
    prompt = llm.prompts["answer"][0]
    assert '"section": "paragraph"' in prompt
    assert "prior work" in prompt and "as its own result" in prompt
    assert "never infer a gap" in prompt
