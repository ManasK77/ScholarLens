"""Related passages alone do not justify an answer (owner brief, 2026-10-19).

A question can be about a topic the corpus touches without the corpus
answering it. Question p086 of the frozen evaluation set asks "Which
tokenization strategies reduce jailbreak success for Arabic prompts?". Paper 1
has an Arabic result and paper 8 mentions tokenization, so retrieval is
sufficient and a draft built on the Arabic passage passes every deterministic
check: its claim is stated by its passage, and the passage names Arabic, one of
the question's subjects. Nothing in those checks asks whether the claim
answers *this* question.

Measured offline on the 130 labelled questions, lexical coverage cannot decide
it: the share of a question's content terms that the retrieved passages carry
is 0.50-1.00 for relevant questions and 0.29-1.00 for weak ones (p086 itself
is 7/7). So the verifier, which already reads the claims and the question,
gives an explicit verdict, `answers_question`. When it says the cited evidence
answers no part of the question, the answer is refused and not regenerated:
regenerating costs two more calls (past the deadline at the deployed pacing)
and cannot add evidence. The LLM is a scripted fake: this is control flow, not
live model behaviour.
"""

from app.interactive.workflow import GroundingVerdict, run_query
from tests.interactive.test_workflow import FakeRetriever, ScriptedLLM, _services

QUESTION = "Which tokenization strategies reduce jailbreak success for Arabic prompts?"
ARABIC = {
    "id": "8184dc4b-c816-5175-9ecb-a2fd238835f7",
    "paper_id": 1,
    "title": "Hidden Prompt Injection in LLM-Based Academic Reviewing",
    "source_type": "paragraph",
    "text": (
        "Table 2 summarizes decision-level outcome changes induced by prompt "
        "injection.For English, Japanese, and Chinese injections, decision outcomes "
        "change for the vast majority of papers, with harsher decisions "
        "overwhelmingly dominating.Arabic injection produces fewer decision changes "
        "overall and exhibits a more balanced distribution of harsher and more "
        "lenient shifts."
    ),
    "score": 0.93,
}
SENTRA = {
    "id": "3a52d238-330c-5be2-b54d-86b8d4e2ed62",
    "paper_id": 8,
    "title": "Sentra-Guard: A Real-Time Multilingual Defense",
    "source_type": "paragraph",
    "text": (
        "To address RQ2, Sentra-Guard was evaluated across five high-frequency "
        "languages: English, French, Spanish, Arabic, and Hindi, targeting diverse "
        "jailbreak scenarios across four major LLM backends: GPT-4o, Gemini Flash, "
        "Claude 3 Opus, and Mistral 7B.Results are reported in Table IV."
    ),
    "score": 0.92,
}
CLAIM = {
    "text": (
        "Arabic injection produces fewer decision changes overall than English, "
        "Japanese, and Chinese injections."
    ),
    "evidence_ids": [ARABIC["id"]],
}
DRAFT = {"answer": "x", "claims": [CLAIM]}
SUPPORTED = {"grounded": True, "unsupported_claim_indexes": []}
NOT_ANSWERING = {**SUPPORTED, "answers_question": False}
ANSWERING = {**SUPPORTED, "answers_question": True}


def _run(llm, question=QUESTION):
    return run_query(
        question,
        _services(FakeRetriever([[ARABIC, SENTRA]]), llm),
        3,
        12,
        2,
        2,
        0.2,
        1,
        min_papers_paper_specific=1,
    )


def test_the_draft_passes_every_deterministic_check():
    """The premise: only the verifier's verdict stands between it and an answer."""
    llm = ScriptedLLM(answer=[DRAFT], verify=[SUPPORTED])

    result = _run(llm)

    assert result["outcome"] == "grounded_answer"
    assert result["claims"] == [CLAIM]


def test_a_verifier_verdict_without_the_field_changes_nothing():
    assert GroundingVerdict.model_validate(SUPPORTED).answers_question is True


def test_evidence_that_does_not_answer_the_question_is_refused():
    llm = ScriptedLLM(answer=[DRAFT], verify=[NOT_ANSWERING])

    result = _run(llm)

    assert result["outcome"] == "grounding_failed"
    assert result["grounded"] is False
    assert result["claims"] == [] and result["citations"] == []
    assert "related to the question" in result["answer"]
    assert result["answer"].startswith("A grounded answer could not be produced")


def test_the_refusal_is_not_regenerated():
    llm = ScriptedLLM(answer=[DRAFT], verify=[NOT_ANSWERING])

    result = _run(llm)

    assert result["regeneration_count"] == 0
    assert len(llm.prompts["answer"]) == 1 and len(llm.prompts["verify"]) == 1
    grounding = next(t for t in result["trace"] if t.startswith("grounding:"))
    assert "reason=not_answerable" in grounding and "kept=0" in grounding


def test_the_refusal_holds_even_when_the_verdict_calls_the_claims_supported():
    llm = ScriptedLLM(answer=[DRAFT], verify=[{**NOT_ANSWERING, "grounded": True}])

    assert _run(llm)["outcome"] == "grounding_failed"


def test_an_answering_verdict_is_a_grounded_answer():
    llm = ScriptedLLM(answer=[DRAFT], verify=[ANSWERING])

    assert _run(llm)["outcome"] == "grounded_answer"


def test_the_verifier_is_told_what_counts_as_answering():
    llm = ScriptedLLM(answer=[DRAFT], verify=[SUPPORTED])

    _run(llm)

    instructions = llm.prompts["verify"][0].split("\n\nQUESTION:\n", 1)[0]
    assert "answers_question" in instructions
    assert "mention" in instructions and "related" in instructions


def _grounding_line(result):
    return next(t for t in result["trace"] if t.startswith("grounding:"))


def test_the_trace_records_the_verifiers_answers_question_verdict():
    """Live LV-R3 let a related answer through and the trace could not say why."""
    answering = _run(ScriptedLLM(answer=[DRAFT], verify=[ANSWERING]))
    refusing = _run(ScriptedLLM(answer=[DRAFT], verify=[NOT_ANSWERING]))

    assert "answers=1" in _grounding_line(answering)
    assert "answers=0" in _grounding_line(refusing)


def test_the_trace_marks_a_draft_rejected_before_the_verifier_ran():
    vetoed = {
        "answer": "x",
        "claims": [{"text": "Only 9000 systems.", "evidence_ids": [ARABIC["id"]]}],
    }
    llm = ScriptedLLM(answer=[vetoed, vetoed], verify=[])

    result = _run(llm)

    assert "answers=-" in _grounding_line(result)


def test_the_verifier_is_told_to_check_what_the_question_names():
    """Live LV-R3: claims about defenses in general answered a question that
    named sandboxing for browsing agents, and the verdict stayed true."""
    llm = ScriptedLLM(answer=[DRAFT], verify=[SUPPORTED])

    _run(llm)

    instructions = llm.prompts["verify"][0].split("\n\nQUESTION:\n", 1)[0]
    assert "names" in instructions and "general topic" in instructions
