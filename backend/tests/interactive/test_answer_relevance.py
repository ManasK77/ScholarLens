"""An answer must address what the question asks about (live LV-Q3, 2026-10-18).

LV-Q3 asked "What accuracy does the Turkish prompt injection benchmark report
for GPT-4 on Turkish prompts?". The benchmark (paper 15) has no indexed
passage, so the right outcome is a refusal; Phase 13's LV-3 refused it. This
time the first draft was vetoed and the strict regeneration returned, as a
grounded answer, a claim its passage does state but that is about a phishing
study (paper 20): "GPT-4o has an accuracy of 95%, Claude Sonnet 4 reaches 94%,
and Grok-3 records the lowest accuracy at 88%." Support was checked; whether
the answer addresses the question was not, and the verifier never saw the
question.

Now an answer is refused when its claims and their cited passages mention
none of the question's subjects: the named systems, models or datasets it
asks about and the languages or resource levels it names (`intent.py`). Short
all-caps acronyms (venues such as WMT or ICML) are not subjects. A question
with no subject is unaffected. The verifier now also reads the question and
is told that a supported claim about a different setting does not answer it.
"""

import pytest

from app.interactive.intent import addresses_question, question_subjects
from app.interactive.workflow import _verification_prompt, run_query
from tests.interactive.test_workflow import FakeRetriever, ScriptedLLM, _services

QUESTION = (
    "What accuracy does the Turkish prompt injection benchmark report for GPT-4 on "
    "Turkish prompts?"
)
PHISHING = {
    "id": "3322a841-f7cd-510c-a407-95b61ee21650",
    "paper_id": 20,
    "title": "Phishing Email Detection Using Large Language Models",
    "text": (
        "We evaluate the balanced dataset using the Universal Structured Prompt "
        "illustrated in Figure 2. GPT-4o has an accuracy of 95%, Claude Sonnet 4 "
        "reaches 94%, and Grok-3 records the lowest accuracy at 88%."
    ),
    "source_type": "paragraph",
    "score": 0.93,
}
TURKISH = {
    "id": "b77a41e9",
    "paper_id": 3,
    "title": "An Empirical Evaluation of Prompt Injection Vulnerabilities",
    "text": "Turkish shows the highest full compliance (80.18%) of the four languages.",
    "source_type": "paragraph",
    "score": 0.93,
}
LIVE_CLAIM = {
    "text": (
        "GPT‑4o has an accuracy of 95%, Claude Sonnet 4 reaches 94%, and Grok-3 "
        "records the lowest accuracy at 88%."
    ),
    "evidence_ids": [PHISHING["id"]],
}
OK = {"grounded": True, "unsupported_claim_indexes": []}


def _run(llm, question=QUESTION, evidence=(PHISHING, TURKISH)):
    return run_query(
        question,
        _services(FakeRetriever([list(evidence)]), llm),
        3,
        12,
        2,
        2,
        0.2,
        1,
        min_papers_paper_specific=1,
    )


def test_the_live_lvq3_off_question_answer_is_refused():
    llm = ScriptedLLM(
        answer=[{"answer": "x", "claims": [LIVE_CLAIM]}] * 2, verify=[OK] * 2
    )
    result = _run(llm)
    assert result["outcome"] == "grounding_failed"
    assert result["claims"] == [] and result["citations"] == []
    # Refused before a verification call was spent on it.
    assert "verify" not in llm.prompts
    grounding = [t for t in result["trace"] if t.startswith("grounding:")]
    assert all("reason=off_question" in g for g in grounding)
    feedback = llm.prompts["answer"][1].split("PREVIOUS_DRAFT_PROBLEMS", 1)[1]
    assert "GPT-4" in feedback and "turkish" in feedback.casefold()


def test_an_answer_about_one_of_the_questions_subjects_is_kept():
    claim = {
        "text": "Turkish shows the highest full compliance (80.18%) of the four languages.",
        "evidence_ids": [TURKISH["id"]],
    }
    llm = ScriptedLLM(answer=[{"answer": "x", "claims": [claim]}], verify=[OK])
    assert _run(llm)["outcome"] == "grounded_answer"


def test_withholding_the_only_on_question_claim_refuses_the_rest():
    turkish = {
        "text": "Turkish shows the highest full compliance (80.18%) of the four languages.",
        "evidence_ids": [TURKISH["id"]],
    }
    llm = ScriptedLLM(
        answer=[{"answer": "x", "claims": [LIVE_CLAIM, turkish]}] * 2,
        verify=[{"grounded": False, "unsupported_claim_indexes": [1]}] * 2,
    )
    result = _run(llm)
    assert result["outcome"] == "grounding_failed"
    assert "reason=off_question" in " ".join(result["trace"])


def test_a_question_without_subjects_is_not_constrained():
    llm = ScriptedLLM(answer=[{"answer": "x", "claims": [LIVE_CLAIM]}], verify=[OK])
    result = _run(llm, question="Which models were most accurate in the evaluation?")
    assert result["outcome"] == "grounded_answer"


def test_the_verifier_reads_the_question_and_the_relevance_rule():
    claims = [{"text": "A.", "evidence_ids": ["e"]}]
    prompt = _verification_prompt("A.", claims, [], question=QUESTION)
    assert "QUESTION:\n" + QUESTION in prompt
    assert "does not answer the QUESTION" in prompt


@pytest.mark.parametrize(
    ("question", "subjects"),
    [
        (QUESTION, ["GPT-4", "turkish"]),
        (
            "Is robustness related to translation quality in the WMT 2024 test suite?",
            [],
        ),
        ("Which datasets are underexplored?", []),
        (
            "What did Sentra-Guard report for low-resource languages?",
            ["Sentra-Guard", "low resource"],
        ),
    ],
)
def test_question_subjects(question, subjects):
    assert question_subjects(question) == subjects


def test_a_named_language_covers_a_non_english_question():
    question = "Is LLM safety weaker for non-English prompts?"
    assert addresses_question(question, ["Turkish prompts had higher compliance."])
    assert not addresses_question(question, ["English prompts were tested."])


def test_gpt_4o_is_not_gpt_4():
    assert not addresses_question("How accurate is GPT-4?", ["GPT-4o reached 95%."])


def test_the_trace_says_whether_a_verification_call_was_made():
    turkish = {
        "text": "Turkish shows the highest full compliance (80.18%) of the four languages.",
        "evidence_ids": [TURKISH["id"]],
    }
    llm = ScriptedLLM(
        answer=[
            {"answer": "x", "claims": [LIVE_CLAIM, turkish]},
            {"answer": "x", "claims": [LIVE_CLAIM]},
        ],
        verify=[{"grounded": False, "unsupported_claim_indexes": [1]}],
    )
    result = _run(llm)
    grounding = [t for t in result["trace"] if t.startswith("grounding:")]
    assert "verified=1" in grounding[0] and "verified=0" in grounding[1]
