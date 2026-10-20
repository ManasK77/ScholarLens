"""Evidence sufficiency by question scope (owner brief, 2026-10-18).

Mode B used to need qualifying passages from two distinct papers for every
question. That refuses a narrow question one paper answers (held-out p031,
p032: all twelve hits from the paper the question is about) while doing
nothing against off-topic questions, whose hits spread across papers. The
policy now reads the question's scope from its own wording:

* single_paper -- the question points at one paper ("the ... study") or names
  a system a retrieved paper's title names: one paper may suffice;
* comparison / synthesis -- it asks across papers ("which papers", "compare",
  "gaps", "across"): the configured distinct-paper minimum still applies, and a
  comparison of named systems needs evidence naming each of them;
* unspecified -- neither: the configured minimum (the conservative default).

Retrieval and the LLM are scripted fakes: these prove control flow and the
rule, not live retrieval quality.
"""

import pytest

from app.interactive.intent import (
    COMPARISON,
    SINGLE_PAPER,
    SYNTHESIS,
    UNSPECIFIED,
    question_scope,
    uncovered_subjects,
)
from app.interactive.workflow import assess_question, run_query
from tests.interactive.test_workflow import FakeRetriever, ScriptedLLM, _services

TEXT = "ChatGPT safety checks fail for non-English prompts such as Turkish."
OK = {"grounded": True, "unsupported_claim_indexes": []}


def _hit(point_id, paper_id, title=None, text=TEXT, score=0.95):
    return {
        "id": point_id,
        "paper_id": paper_id,
        "title": title,
        "text": text,
        "score": score,
        "source_type": "paragraph",
    }


def _answer(*ids):
    return {"answer": "x", "claims": [{"text": TEXT, "evidence_ids": list(ids)}]}


def _query(question, retriever, llm, *, specific=1, **overrides):
    options = {
        "max_rounds": 3,
        "retrieval_limit": 12,
        "min_relevant_chunks": 2,
        "min_distinct_papers": 2,
        "min_relevance_score": 0.2,
        "max_regenerations": 1,
        "min_papers_paper_specific": specific,
    }
    options.update(overrides)
    return run_query(question, _services(retriever, llm), **options)


# --- scope from the question's own wording ----------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "How many languages were covered in the ChatGPT multilingual fuzzing study?",
        (
            "Which languages were used for the hidden prompt injection in the "
            "LLM-based academic reviewing study?"
        ),
        "What future work does the MIPIAD paper propose?",
        "What accuracy did this evaluation report on HarmBench-28K?",
    ],
)
def test_a_question_about_one_paper_is_single_paper(question):
    assert question_scope(question) == SINGLE_PAPER


@pytest.mark.parametrize(
    "question",
    [
        "Which papers report that safety is weaker for non-English prompts?",
        "What remains unsolved in multilingual prompt injection?",
        "Which datasets are underexplored?",
        "Where do recent findings contradict each other?",
        "What limitations do existing studies report about language coverage?",
        "How is prompt injection evaluated across the literature?",
        "Which open problems remain in multilingual jailbreak defense?",
        # A singular reference does not win against a plural one.
        "Do other papers confirm the result of the reviewing study?",
    ],
)
def test_a_question_across_papers_is_synthesis(question):
    assert question_scope(question) == SYNTHESIS


@pytest.mark.parametrize(
    "question",
    [
        "Compare MIPIAD and Sentra-Guard on multilingual detection.",
        "How do MIPIAD and Sentra-Guard differ in the languages they cover?",
        "Is Sentra-Guard more accurate than MIPIAD versus code-mixed prompts?",
    ],
)
def test_a_comparison_of_two_named_systems_is_comparison(question):
    assert question_scope(question) == COMPARISON


@pytest.mark.parametrize(
    "question",
    [
        "Does prompt injection increase jailbreak probability for multilingual questions?",
        "What is the best way to brew espresso?",
    ],
)
def test_a_question_with_no_scope_signal_is_unspecified(question):
    assert question_scope(question) == UNSPECIFIED


def test_a_model_a_title_only_mentions_keeps_the_conservative_default():
    # Code review (2026-10-18): a title that evaluates ChatGPT does not make a
    # question about ChatGPT one paper's question (held-out p032 stays refused).
    question = "Does prompt injection increase jailbreak probability for ChatGPT?"
    title = (
        "Comprehensive Evaluation of ChatGPT Reliability Through Multilingual Inquiries"
    )
    assert question_scope(question, [title]) == UNSPECIFIED
    # A title that does not name it is no signal either.
    assert (
        question_scope(question, ["Sentra-Guard: A Real-Time Defense"]) == UNSPECIFIED
    )


def test_comparison_subjects_must_each_be_named_by_the_evidence():
    question = "Compare MIPIAD and Sentra-Guard on multilingual detection."
    only_one = [{"text": "MIPIAD detects Bangla injections.", "title": None}]
    both = [*only_one, {"text": "It detects 96%.", "title": "Sentra-Guard: Defense"}]
    assert uncovered_subjects(question, only_one) == ["Sentra-Guard"]
    assert uncovered_subjects(question, both) == []


# --- the gate applies the scope's paper minimum ------------------------------------


ONE_PAPER = [_hit("a", 10), _hit("b", 10, text="Another statement from paper ten.")]


def test_one_strong_paper_suffices_for_a_single_paper_question():
    verdict = assess_question(
        "How many languages were covered in the ChatGPT multilingual fuzzing study?",
        ONE_PAPER,
        0.2,
        2,
        2,
        1,
    )
    assert verdict["sufficient"] is True
    assert verdict["scope"] == SINGLE_PAPER and verdict["required_papers"] == 1


@pytest.mark.parametrize(
    "question",
    [
        "Which papers report that safety is weaker for non-English prompts?",
        "Does prompt injection increase jailbreak probability for multilingual questions?",
    ],
    ids=["synthesis", "unspecified"],
)
def test_one_paper_is_not_enough_for_a_synthesis_or_unspecified_question(question):
    verdict = assess_question(question, ONE_PAPER, 0.2, 2, 2, 1)
    assert verdict["sufficient"] is False and verdict["required_papers"] == 2


def test_a_single_paper_question_still_needs_the_minimum_number_of_passages():
    question = "What future work does the MIPIAD paper propose?"
    graph = {"id": "graph:2:x", "paper_id": 2, "text": "MIPIAD | Model", "score": 0.5}
    verdict = assess_question(
        question, [_hit("a", 2), {**graph, "source_type": "graph"}], 0.2, 2, 2, 1
    )
    # One passage plus a graph row: graph rows never count (D-10-05).
    assert verdict["sufficient"] is False and verdict["relevant"] == 1


def test_a_single_paper_question_still_needs_passages_above_the_threshold():
    question = "What future work does the MIPIAD paper propose?"
    weak = [_hit("a", 2, score=0.1), _hit("b", 2, text="Other.", score=0.1)]
    assert assess_question(question, weak, 0.2, 2, 2, 1)["sufficient"] is False


def test_a_comparison_missing_one_named_subject_is_insufficient():
    question = "Compare MIPIAD and Sentra-Guard on multilingual detection."
    hits = [
        _hit("a", 2, title="MIPIAD: Multilingual Defense"),
        _hit("b", 3, title="An Empirical Evaluation"),
    ]
    verdict = assess_question(question, hits, 0.2, 2, 2, 1)
    assert verdict["scope"] == COMPARISON
    assert verdict["sufficient"] is False and verdict["uncovered_subjects"] == 1
    covered = [*hits, _hit("c", 8, title="Sentra-Guard: Real-Time Defense")]
    assert assess_question(question, covered, 0.2, 2, 2, 1)["sufficient"] is True


def test_without_a_paper_specific_minimum_every_scope_keeps_the_configured_one():
    question = (
        "How many languages were covered in the ChatGPT multilingual fuzzing study?"
    )
    verdict = assess_question(question, ONE_PAPER, 0.2, 2, 2, None)
    assert verdict["sufficient"] is False and verdict["required_papers"] == 2


# --- end to end through the compiled graph -----------------------------------------


def test_a_single_paper_question_is_answered_without_reformulation():
    question = (
        "How many languages were covered in the ChatGPT multilingual fuzzing study?"
    )
    retriever = FakeRetriever([ONE_PAPER])
    llm = ScriptedLLM(answer=[_answer("a")], verify=[OK])
    result = _query(question, retriever, llm)
    assert result["outcome"] == "grounded_answer"
    assert result["retrieval_rounds"] == 1 and "reformulate" not in llm.prompts
    assessment = next(t for t in result["trace"] if t.startswith("assess:"))
    assert "scope=single_paper" in assessment and "required_papers=1" in assessment


def test_a_synthesis_question_answered_by_one_paper_reformulates_then_refuses():
    question = "Which papers report that safety is weaker for non-English prompts?"
    retriever = FakeRetriever([ONE_PAPER])
    llm = ScriptedLLM(reformulate=[{"query": "non-English safety papers"}] * 2)
    result = _query(question, retriever, llm)
    assert result["outcome"] == "insufficient_evidence"
    assert result["retrieval_rounds"] == 3 and len(llm.prompts["reformulate"]) == 2
    assert "answer" not in llm.prompts
    assert result["claims"] == [] and result["citations"] == []


def test_the_scope_is_read_from_the_question_not_the_reformulated_query():
    # Reformulation rewrites the retrieval query, never the user's intent.
    question = "Which papers report that safety is weaker for non-English prompts?"
    retriever = FakeRetriever([ONE_PAPER])
    llm = ScriptedLLM(reformulate=[{"query": "the MIPIAD paper"}] * 2)
    result = _query(question, retriever, llm)
    assessments = [t for t in result["trace"] if t.startswith("assess:")]
    assert len(assessments) == 3 and all("scope=synthesis" in a for a in assessments)
    assert retriever.queries[1][0] == "the MIPIAD paper"


def test_reformulation_that_finds_a_second_paper_rescues_a_synthesis_question():
    question = "Which papers report that safety is weaker for non-English prompts?"
    second = [*ONE_PAPER, _hit("c", 3)]
    retriever = FakeRetriever([ONE_PAPER, second])
    llm = ScriptedLLM(
        reformulate=[{"query": "non-English safety compliance"}],
        answer=[_answer("a")],
        verify=[OK],
    )
    result = _query(question, retriever, llm)
    assert result["outcome"] == "grounded_answer" and result["retrieval_rounds"] == 2


def test_run_query_defaults_keep_the_two_paper_rule_for_every_question():
    question = (
        "How many languages were covered in the ChatGPT multilingual fuzzing study?"
    )
    retriever = FakeRetriever([ONE_PAPER])
    llm = ScriptedLLM(reformulate=[{"query": "q2"}] * 2)
    result = run_query(question, _services(retriever, llm), 3, 12, 2, 2, 0.2, 1)
    assert result["outcome"] == "insufficient_evidence"


# --- code-review follow-ups (2026-10-18) ---------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "What open problems remain in the evaluation of prompt injection defenses?",
        "Compare the BIPIA benchmark with other benchmarks.",
        "Which gaps does the multilingual evaluation leave open?",
    ],
)
def test_a_generic_definite_noun_does_not_override_synthesis_wording(question):
    assert question_scope(question) == SYNTHESIS


def test_a_paper_or_study_reference_still_wins_over_gap_wording():
    assert (
        question_scope("What future work does the MIPIAD paper propose?")
        == SINGLE_PAPER
    )
    assert question_scope("Which gaps does the reviewing study report?") == SINGLE_PAPER


def test_a_model_named_in_a_title_does_not_make_a_broad_question_single_paper():
    question = "How robust is GPT-4o to indirect prompt injection?"
    titles = ["Phishing Email Detection Using GPT-4o and Other LLMs"]
    assert question_scope(question, titles) == UNSPECIFIED


def test_the_system_a_title_introduces_does_make_it_single_paper():
    question = "How does Sentra-Guard handle code-mixed prompts?"
    titles = [
        "Sentra-Guard: A Real-Time Multilingual Defense Against Adversarial LLM Prompts"
    ]
    assert question_scope(question, titles) == SINGLE_PAPER


def test_a_venue_acronym_is_not_a_comparison_subject():
    question = "Compare MIPIAD and Sentra-Guard on WMT."
    passages = [
        {"text": "MIPIAD detects Bangla injections.", "title": None},
        {"text": "It detects 96%.", "title": "Sentra-Guard: Defense"},
    ]
    assert uncovered_subjects(question, passages) == []
