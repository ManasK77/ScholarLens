"""The S1 final relevance gate (D-S1-GATE-01).

Source search results are candidates, not proof of relevance. The first clean
baseline admitted "A Comprehensive Evaluation of Multilingual Chain-of-Thought
Reasoning" for the topic "multilingual prompt injection" because its abstract
said "error injection"; the ACL rule required each topic word as a substring
and nothing checked what the words meant together.

The gate decides from four kinds of evidence, each recorded:

* phrase evidence   -- which topic phrases occur verbatim;
* concept evidence  -- every facet of the topic, matched through the
  versioned concept lexicon (or word-boundary matching for unknown words);
* negative evidence -- a facet's head word used in a different sense
  ("error injection" for "prompt injection");
* semantic evidence -- a cross-encoder relevance score against a floor.

No test loads a model: the semantic scorer is a fake that returns fixed
scores, so these tests pin the decision logic, not the model.
"""

import pytest

from app.ingestion.relevance import (
    GATE_VERSION,
    RelevanceGate,
    analyze_topic,
    load_lexicon,
    normalize,
)
from tests.ingestion.gate_fakes import FakeScorer

TOPIC = "multilingual prompt injection"


def _gate(topic=TOPIC, scorer=None, floor=-1.0, strong=3.5):
    return RelevanceGate(
        analyze_topic(topic, load_lexicon()),
        scorer or FakeScorer(),
        semantic_floor=floor,
        strong_threshold=strong,
    )


def _one(gate, title, abstract=None):
    [decision] = gate.evaluate_many([(title, abstract)])
    return decision


# ---------------------------------------------------------------------------
# Topic analysis and the lexicon
# ---------------------------------------------------------------------------


class TestTopicAnalysis:
    def test_the_topic_splits_into_its_two_concept_facets(self):
        analysis = analyze_topic(TOPIC, load_lexicon())

        assert [(f.name, f.kind) for f in analysis.facets] == [
            ("multilingual", "concept"),
            ("prompt injection", "concept"),
        ]

    def test_alternative_terminology_in_the_topic_maps_to_the_same_concept(self):
        analysis = analyze_topic("Cross-lingual prompt injections", load_lexicon())

        assert [f.name for f in analysis.facets] == ["multilingual", "prompt injection"]

    def test_unknown_words_become_word_facets(self):
        analysis = analyze_topic("federated unlearning", load_lexicon())

        assert [(f.name, f.kind) for f in analysis.facets] == [
            ("federated", "word"),
            ("unlearning", "word"),
        ]

    def test_the_lexicon_is_versioned(self):
        assert load_lexicon().version
        assert GATE_VERSION

    def test_normalization_joins_hyphenated_and_latex_protected_text(self):
        assert normalize("{S}hef{F}riday: Prompt-Injection--Based") == (
            "sheffriday: prompt injection based"
        )


# ---------------------------------------------------------------------------
# The required cases
# ---------------------------------------------------------------------------


class TestRequiredCases:
    def test_error_injection_is_a_sense_mismatch_not_prompt_injection(self):
        """Paper 18 of the baseline, verbatim in substance."""
        decision = _one(
            _gate(),
            "A Comprehensive Evaluation of Multilingual Chain-of-Thought Reasoning",
            "We evaluate multilingual reasoning with prompt perturbations, "
            "truncation and error injection to probe faithfulness across languages.",
        )

        assert not decision.admitted
        assert decision.reason_code == "sense_mismatch:prompt injection"
        assert "error injection" in decision.reason
        assert decision.evidence["negative"]["prompt injection"] == ["error injection"]

    def test_multilingual_jailbreak_without_prompt_injection_is_rejected(self):
        decision = _one(
            _gate(),
            "Mātṛkā: Multilingual Jailbreak Evaluation of Open-Source LLMs",
            "We evaluate jailbreak attacks across Hindi, Tamil and English.",
        )

        assert not decision.admitted
        assert decision.reason_code == "missing_facet:prompt injection"

    def test_multilingual_indirect_prompt_injection_is_admitted(self):
        decision = _one(
            _gate(),
            "MIPIAD: Multilingual Indirect Prompt Injection Attack Defense",
            "A pipeline for cross-lingual detection of injected instructions.",
        )

        assert decision.admitted
        assert decision.reason_code == "relevant:strong"

    def test_cross_lingual_prompt_injection_is_admitted_without_the_word_multilingual(
        self,
    ):
        decision = _one(
            _gate(),
            "Cross-Lingual Indirect Prompt Injection Across Retrieval and Generation",
            "Attacks planted in one language hijack answers in another.",
        )

        assert decision.admitted
        assert decision.evidence["facets"]["multilingual"]["matched"] == [
            "cross lingual"
        ]

    def test_tamil_tanglish_prompt_injection_is_admitted_through_language_names(self):
        decision = _one(
            _gate(scorer=FakeScorer(default=-0.27)),
            "Visible Reasoning and Indirect Prompt-Injection Monitorability Across "
            "English, Tamil, and Tanglish",
            "We study IPI behaviour in Sarvam-105B.",
        )

        assert decision.admitted
        assert decision.reason_code == "relevant:conceptual"
        facet = decision.evidence["facets"]["multilingual"]
        assert facet["matched"] == []
        assert facet["language_names"] == ["english", "tamil", "tanglish"]

    def test_machine_translation_prompt_injection_is_admitted(self):
        decision = _one(
            _gate(scorer=FakeScorer(default=0.71)),
            "Scaling Behavior of Machine Translation with Large Language Models under "
            "Prompt Injection Attacks",
            "Injected instructions in the source text redirect the translation.",
        )

        assert decision.admitted
        assert (
            "machine translation"
            in decision.evidence["facets"]["multilingual"]["matched"]
        )

    def test_the_exact_topic_phrase_is_recorded_as_phrase_evidence(self):
        decision = _one(
            _gate(),
            "Detection of multilingual prompt injection",
            "We detect multilingual prompt injections in chat assistants.",
        )

        assert decision.admitted
        assert decision.evidence["phrase"]["exact_topic_phrase"] is True
        assert (
            "prompt injection" in decision.evidence["phrase"]["topic_phrases_present"]
        )


class TestMissingText:
    def test_a_title_only_candidate_is_judged_on_its_title(self):
        scorer = FakeScorer()
        decision = _one(
            _gate(scorer=scorer),
            "Multilingual Prompt-Injection-Based Jailbreak Vulnerability Analysis",
            None,
        )

        assert decision.admitted
        assert decision.evidence["text_used"] == "title"
        assert scorer.calls[0][1] == [
            "Multilingual Prompt-Injection-Based Jailbreak Vulnerability Analysis"
        ]

    def test_a_title_only_candidate_missing_a_facet_is_insufficient_text(self):
        """Without an abstract, absence from the title is not proof of absence."""
        decision = _one(_gate(), "Multilingual Safety of Large Language Models", "")

        assert not decision.admitted
        assert decision.reason_code == "insufficient_text"
        assert "prompt injection" in decision.reason

    def test_no_title_and_no_abstract_is_insufficient_text(self):
        decision = _one(_gate(), None, None)

        assert not decision.admitted
        assert decision.reason_code == "insufficient_text"


class TestSemanticFloor:
    def test_all_facets_but_a_score_below_the_floor_is_rejected(self):
        decision = _one(
            _gate(scorer=FakeScorer(default=-6.58), floor=-1.0),
            "Safe-Unsafe Concept Separation in Activation Space",
            "Jailbreaks and prompt injection across multilingual inputs share one direction.",
        )

        assert not decision.admitted
        assert decision.reason_code == "low_semantic_relevance"
        assert decision.score == pytest.approx(-6.58)

    def test_the_floor_is_configurable(self):
        text = ("Multilingual prompt injection", "An abstract about it.")

        assert not _one(
            _gate(scorer=FakeScorer(default=0.5), floor=1.0), *text
        ).admitted
        assert _one(_gate(scorer=FakeScorer(default=0.5), floor=0.0), *text).admitted

    def test_the_semantic_model_is_only_asked_about_candidates_with_every_facet(self):
        scorer = FakeScorer()
        _gate(scorer=scorer).evaluate_many(
            [
                ("Multilingual jailbreaks", "No injection here."),
                ("Multilingual prompt injection", "Yes."),
                ("Error injection in multilingual reasoning", "Faithfulness."),
            ]
        )

        assert len(scorer.calls) == 1
        assert scorer.calls[0][1] == ["Multilingual prompt injection. Yes."]
        assert scorer.calls[0][0] == TOPIC

    def test_a_rejected_candidate_before_the_semantic_step_has_no_score(self):
        decision = _one(_gate(), "Multilingual jailbreaks", "An abstract.")

        assert decision.score is None


class TestWordFacets:
    def test_unknown_words_match_on_word_boundaries_not_substrings(self):
        gate = _gate("federated unlearning")

        assert not _one(gate, "Nonfederated relearning", "Nothing relevant.").admitted
        assert _one(gate, "Federated unlearning at scale", "Clients unlearn.").admitted

    def test_word_facets_accept_a_plural(self):
        gate = _gate("graph transformer")

        assert _one(gate, "Graph transformers", "Two graphs.").admitted


class TestAuditability:
    def test_every_decision_carries_a_reason_and_the_full_evidence(self):
        decision = _one(_gate(), "Multilingual prompt injection", "Abstract.")

        assert decision.reason
        assert set(decision.evidence) >= {
            "phrase",
            "facets",
            "negative",
            "semantic",
            "text_used",
        }
        assert decision.evidence["semantic"]["floor"] == -1.0

    def test_other_senses_are_recorded_even_when_the_facet_is_present(self):
        decision = _one(
            _gate(),
            "Multilingual prompt injection versus fault injection",
            "Both are studied.",
        )

        assert decision.admitted
        assert decision.evidence["negative"]["prompt injection"] == ["fault injection"]
