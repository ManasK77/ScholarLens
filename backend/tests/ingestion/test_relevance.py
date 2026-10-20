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
    RelevanceDecision,
    RelevanceGate,
    analyze_topic,
    load_lexicon,
    normalize,
    rank_key,
)
from tests.ingestion.gate_fakes import FakeScorer
from tests.ingestion.test_paper_language import (
    ENGLISH_ABOUT_TAMIL,
    ENGLISH_WITH_FOREIGN_TERMS,
    FRENCH,
    TAMIL_SCRIPT,
)

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


# ---------------------------------------------------------------------------
# Meaningful evidence (S1 audit, 2026-10-10)
#
# Gate v1 counted a facet as evidenced on any occurrence of one of its forms.
# The audit found three ways that goes wrong -- "prompt injection" in a benign
# technique sense, "multilingual" naming only an encoder, multilinguality raised
# only as future work -- plus jailbreak papers that mention prompt injection
# once in passing. Each is now a recorded rejection of its own.
# ---------------------------------------------------------------------------


class TestMeaningfulEvidence:
    def test_prompt_injection_as_a_training_technique_is_a_benign_sense(self):
        decision = _one(
            _gate(),
            "Adapting language models to low-resource languages",
            "We combine soft prompt tuning with prompt injection and layer freezing "
            "to preserve the model's knowledge during fine-tuning.",
        )

        assert not decision.admitted
        assert decision.reason_code == "benign_sense:prompt injection"
        assert decision.evidence["facets"]["prompt injection"]["benign_sense"]

    def test_a_technique_context_with_any_threat_context_is_not_benign(self):
        decision = _one(
            _gate(),
            "Adapting language models to low-resource languages",
            "We study prompt injection attacks against models adapted with soft "
            "prompt tuning.",
        )

        assert decision.admitted

    def test_multilinguality_raised_only_as_future_work_is_not_evidence(self):
        decision = _one(
            _gate(),
            "Prompt injection vulnerabilities across 36 language models",
            "We run 144 prompt injection attacks. Future research should explore "
            "multilingual and multi-step defenses.",
        )

        assert not decision.admitted
        assert decision.reason_code == "future_work_only:multilingual"
        facet = decision.evidence["facets"]["multilingual"]
        assert facet["present"] is False
        assert facet["discounted"]["future_work"] == ["multilingual"]

    def test_future_work_does_not_discount_evidence_found_elsewhere(self):
        decision = _one(
            _gate(),
            "Prompt injection in Hindi and Tamil assistants",
            "We inject instructions in Hindi and Tamil. Future work will cover "
            "more multilingual settings.",
        )

        assert decision.admitted

    def test_multilingual_naming_only_an_encoder_is_an_incidental_mention(self):
        decision = _one(
            _gate(),
            "Detecting prompt injection with multilingual BERT embeddings",
            "We classify malicious prompt injection attacks using multilingual "
            "BERT embeddings and logistic regression.",
        )

        assert not decision.admitted
        assert decision.reason_code == "incidental_mention:multilingual"
        assert decision.evidence["facets"]["multilingual"]["discounted"][
            "incidental"
        ] == ["multilingual"]

    def test_an_encoder_name_does_not_discount_real_multilingual_evidence(self):
        decision = _one(
            _gate(),
            "Cross-lingual prompt injection detection",
            "We detect prompt injection attacks written in Bangla and English "
            "with a multilingual BERT encoder.",
        )

        assert decision.admitted

    def test_a_multilingual_embedding_model_under_attack_is_the_topic(self):
        decision = _one(
            _gate(),
            "Prompt injection attacks on multilingual embedding models in RAG",
            "We show that attackers can inject malicious instructions into "
            "documents so a multilingual embedding model retrieves them, a "
            "prompt injection attack that works in 10 languages.",
        )

        assert decision.admitted

    def test_a_multilingual_encoder_under_attack_is_the_topic(self):
        decision = _one(
            _gate(),
            "Adversarial prompt injection against multilingual encoders",
            "We craft adversarial prompt injection inputs that fool multilingual "
            "encoder classifiers.",
        )

        assert decision.admitted

    def test_a_multilingual_embedding_used_as_a_component_stays_incidental(self):
        decision = _one(
            _gate(),
            "Detecting prompt injection with multilingual embeddings",
            "We classify prompts as injected or clean. Features are multilingual "
            "embeddings fed to logistic regression.",
        )

        assert not decision.admitted
        assert decision.reason_code == "incidental_mention:multilingual"

    def test_a_tool_name_stays_incidental_even_in_a_sentence_about_attacks(self):
        decision = _one(
            _gate(),
            "Detecting prompt injection with multilingual BERT",
            "We detect malicious prompt injection attacks with multilingual BERT "
            "and logistic regression.",
        )

        assert not decision.admitted
        assert decision.reason_code == "incidental_mention:multilingual"

    def test_an_incidental_title_mention_without_an_abstract_is_insufficient(self):
        decision = _one(_gate(), "Prompt injection detection with multilingual BERT")

        assert not decision.admitted
        assert decision.reason_code == "insufficient_text"

    def test_a_jailbreak_paper_mentioning_prompt_injection_in_passing_is_adjacent(
        self,
    ):
        decision = _one(
            _gate(),
            "Multilingual Jailbreak Prompts on Closed-Source Models",
            "Models remain vulnerable to adversarial prompt injections. We run 32 "
            "jailbreak attacks in English and Chinese; jailbreak success differs "
            "by language.",
        )

        assert not decision.admitted
        assert decision.reason_code == "adjacent_topic:jailbreak"

    def test_a_title_naming_prompt_injection_is_never_adjacent(self):
        decision = _one(
            _gate(),
            "Multilingual Prompt-Injection-Based Jailbreak Vulnerability Analysis",
            "We compare jailbreak success in English and Korean; jailbreak rates "
            "differ by speech style.",
        )

        assert decision.admitted

    def test_jailbreak_is_not_a_neighbour_when_the_topic_asks_for_it(self):
        decision = _one(
            _gate("multilingual jailbreak prompt injection"),
            "Multilingual Jailbreak Prompts on Closed-Source Models",
            "Models remain vulnerable to adversarial prompt injections. We run 32 "
            "jailbreak attacks in English and Chinese.",
        )

        assert decision.admitted


class TestTitleOnlyAdmission:
    def test_a_title_only_candidate_is_admitted_only_in_the_title_only_tier(self):
        decision = _one(
            _gate(scorer=FakeScorer(default=9.0)),
            "Multilingual Prompt-Injection-Based Jailbreak Vulnerability Analysis",
        )

        assert decision.admitted
        assert decision.tier == "title_only"
        assert decision.reason_code == "relevant:title_only"
        assert "no abstract" in decision.reason.lower()


class TestRanking:
    def test_title_only_ranks_below_every_candidate_with_an_abstract(self):
        gate = _gate(
            scorer=FakeScorer(
                by_title={"Multilingual prompt injection": 9.0}, default=0.0
            )
        )
        title_only, weak = gate.evaluate_many(
            [
                ("Multilingual prompt injection", None),
                ("Prompt injection study", "Attacks written in Hindi and Tamil."),
            ]
        )

        assert rank_key(weak) > rank_key(title_only)

    def test_both_facets_in_the_title_outrank_a_higher_score_with_none(self):
        gate = _gate(
            scorer=FakeScorer(
                by_title={
                    "A defense for LLM prompts": 6.0,
                    "Cross-lingual prompt injection": 0.5,
                }
            )
        )
        none_in_title, both = gate.evaluate_many(
            [
                (
                    "A defense for LLM prompts",
                    "Multilingual prompt injection detection.",
                ),
                ("Cross-lingual prompt injection", "Attacks in Hindi and Tamil."),
            ]
        )

        assert both.evidence["title_facets"] == 2
        assert none_in_title.evidence["title_facets"] == 0
        assert rank_key(both) > rank_key(none_in_title)

    def test_equal_title_evidence_falls_back_to_the_semantic_score(self):
        gate = _gate(
            scorer=FakeScorer(
                by_title={"Prompt injection A": 1.0, "Prompt injection B": 2.0}
            )
        )
        a, b = gate.evaluate_many(
            [
                ("Prompt injection A", "In Hindi and Tamil."),
                ("Prompt injection B", "In Hindi and Tamil."),
            ]
        )

        assert rank_key(b) > rank_key(a)


class TestQueryGroups:
    def test_concept_facets_contribute_their_validated_query_forms(self):
        groups = _gate().query_groups()

        assert groups[0][0] == "multilingual"
        assert {"cross-lingual", "code-mixed", "machine translation"} <= set(groups[0])
        # Lexicon 2026-10-12.2: the facet is searched as its phrases, not as
        # the separate words "prompt" AND "injection" (59 arXiv matches with
        # 31 noise records, against 28 for the phrases; the 20 gate-passing
        # records were in both).
        [prompt_injection] = groups[1:]
        assert prompt_injection[0] == "prompt injection"
        assert {"injected instruction", "goal hijacking"} <= set(prompt_injection)

    def test_every_query_form_is_a_form_the_gate_accepts(self):
        """A query phrase the gate would not count as evidence only buys noise."""
        lexicon = load_lexicon()
        for concept in lexicon.concepts:
            for query_form in concept.query_forms:
                normalized = normalize(query_form)
                assert any(
                    normalized == form or normalized in (f"{form}s", f"{form}es")
                    for form in concept.forms
                ), (concept.name, query_form)

    def test_a_word_facet_still_constrains_the_query_on_its_own(self):
        groups = _gate("prompt injection detection").query_groups()

        assert groups[0][0] == "prompt injection"
        assert groups[1:] == [["detection"]]

    def test_query_synonyms_stay_few_and_never_include_bare_language_names(self):
        multilingual = _gate().query_groups()[0]

        assert len(multilingual) <= 10
        assert not set(multilingual) & load_lexicon().language_names
        assert "different languages" not in multilingual

    def test_a_topic_with_no_query_forms_has_no_query_groups(self):
        assert _gate("federated unlearning").query_groups() is None


# ---------------------------------------------------------------------------
# Paper language (D-S1-LANG-01, Phase 1 closeout 2026-10-12)
#
# A corpus paper must be written in English in the Latin alphabet; it may
# study any language. The abstract is the evidence -- never the title -- and
# text too short to judge is recorded as unverified, not rejected.
# ---------------------------------------------------------------------------


class TestPaperLanguage:
    def test_an_english_paper_about_tamil_attacks_is_admitted(self):
        decision = _one(_gate(), "Prompt injection in Tamil", ENGLISH_ABOUT_TAMIL)

        assert decision.admitted
        assert decision.evidence["language"]["status"] == "english"

    def test_foreign_script_terms_in_an_english_abstract_do_not_reject_it(self):
        decision = _one(
            _gate(), "Prompt injection in Tamil", ENGLISH_WITH_FOREIGN_TERMS
        )

        assert decision.admitted
        assert decision.evidence["language"]["status"] == "english"

    def test_an_abstract_in_tamil_script_is_rejected_whatever_the_title(self):
        scorer = FakeScorer()
        decision = _one(
            _gate(scorer=scorer),
            "Multilingual Prompt Injection Attacks in Tamil",
            TAMIL_SCRIPT,
        )

        assert not decision.admitted
        assert decision.reason_code == "paper_language:non_latin_script"
        assert decision.evidence["language"]["status"] == "non_latin_script"
        assert scorer.calls == []

    def test_an_english_title_does_not_make_a_french_abstract_english(self):
        decision = _one(_gate(), "Multilingual Prompt Injection Attacks", FRENCH)

        assert not decision.admitted
        assert decision.reason_code == "paper_language:not_english"

    def test_a_title_only_candidate_is_admitted_with_its_language_unverified(self):
        decision = _one(_gate(), "Multilingual Prompt Injection Attacks")

        assert decision.admitted
        assert decision.tier == "title_only"
        assert decision.evidence["language"]["status"] == "unverified"

    def test_a_short_abstract_keeps_the_relevance_verdict_and_flags_language(self):
        decision = _one(
            _gate(), "Multilingual Prompt Injection Study", "Injected instructions."
        )

        assert decision.admitted
        assert decision.evidence["language"]["status"] == "unverified"

    def test_no_text_is_still_insufficient_text(self):
        decision = _one(_gate(), None, None)

        assert decision.reason_code == "insufficient_text"

    def test_the_gate_describes_its_language_policy(self):
        described = _gate().describe()

        assert described["language_check"]["version"]
        assert described["language_check"]["min_english_ratio"] == 0.10


# ---------------------------------------------------------------------------
# Decision history (Phase 1 closeout, 2026-10-12)
#
# Run 2 recorded capped candidates whose evidence["gate"] said
# "duplicate_of_admitted": promoting a rejected copy and then capping it
# overwrote the gate's own verdict. evidence["gate"] is now always the gate's
# original verdict and every later transition is appended to gate_history.
# ---------------------------------------------------------------------------


def _rejected():
    return RelevanceDecision(
        False,
        "adjacent_topic:jailbreak",
        "The title names 'jailbreak'.",
        evidence={"facets": {"prompt injection": {"present": True}}},
    )


def _admitted():
    return RelevanceDecision(
        True, "relevant:strong", "Evidence for every facet.", score=6.0, tier="strong"
    )


class TestDecisionHistory:
    def test_a_rejected_candidate_promoted_as_a_duplicate_keeps_its_verdict(self):
        promoted = _rejected().as_duplicate_of(4)

        assert promoted.admitted
        assert promoted.reason_code == "duplicate_of_admitted"
        assert promoted.evidence["gate"]["reason_code"] == "adjacent_topic:jailbreak"
        assert promoted.evidence["gate"]["admitted"] is False
        assert [
            (h["step"], h["reason_code"]) for h in promoted.evidence["gate_history"]
        ] == [
            ("gate", "adjacent_topic:jailbreak"),
            ("duplicate_promotion", "duplicate_of_admitted"),
        ]
        assert promoted.evidence["gate_history"][1]["paper_id"] == 4
        assert promoted.evidence["facets"]["prompt injection"]["present"]

    def test_a_capped_candidate_keeps_the_gate_verdict(self):
        capped = _admitted().capped("arxiv", 10)

        assert capped.reason_code == "source_cap_reached"
        assert capped.evidence["gate"]["reason_code"] == "relevant:strong"
        assert [
            (h["step"], h["admitted"]) for h in capped.evidence["gate_history"]
        ] == [("gate", True), ("source_cap", False)]
        assert capped.evidence["gate_history"][1]["source"] == "arxiv"
        assert capped.evidence["gate_history"][1]["limit"] == 10

    def test_promotion_then_capping_never_overwrites_the_original_verdict(self):
        """The run-2 defect: the capped record said only duplicate_of_admitted."""
        capped = _rejected().as_duplicate_of(6).capped("semantic_scholar", 10)

        assert capped.evidence["gate"]["reason_code"] == "adjacent_topic:jailbreak"
        assert [h["reason_code"] for h in capped.evidence["gate_history"]] == [
            "adjacent_topic:jailbreak",
            "duplicate_of_admitted",
            "source_cap_reached",
        ]
        assert "adjacent_topic:jailbreak" in capped.reason

    def test_annotations_keep_the_history(self):
        promoted = _rejected().as_duplicate_of(4).with_evidence(existing_paper_id=4)

        assert len(promoted.evidence["gate_history"]) == 2

    def test_a_gate_verdict_alone_has_no_transition_history(self):
        decision = _one(_gate(), "Multilingual Prompt Injection Study", "Abstract.")

        assert "gate" not in decision.evidence
        assert "gate_history" not in decision.evidence


class TestMissingFacetsAndLanguageLists:
    """2026-10-12: each facet must be evidenced on its own; a list of
    languages evidences only the multilingual facet, and only that."""

    def test_a_multilingual_paper_without_prompt_injection_is_missing_a_facet(self):
        decision = _one(
            _gate(),
            "A Multilingual Machine Translation Benchmark",
            "We release a benchmark covering Hindi, Tamil, Turkish and Arabic "
            "for evaluating translation quality of large language models.",
        )

        assert decision.reason_code == "missing_facet:prompt injection"

    def test_a_long_language_list_does_not_stand_in_for_prompt_injection(self):
        decision = _one(
            _gate(),
            "A Survey of Copyright and AI Technology",
            "We review copyright cases in Chinese, English, French, German, "
            "Italian, Japanese, Portuguese and Spanish courts.",
        )

        assert decision.reason_code == "missing_facet:prompt injection"
        assert decision.evidence["facets"]["multilingual"]["evidenced_by"] == (
            "language_names"
        )

    def test_a_language_list_with_a_passing_mention_is_left_to_the_semantic_floor(
        self,
    ):
        scorer = FakeScorer(default=-5.0)
        decision = _one(
            _gate(scorer=scorer),
            "Governance of AI Products",
            "Our product is available in Chinese, English and French. We list "
            "risks including prompt injection, data leakage and misuse.",
        )

        assert decision.reason_code == "low_semantic_relevance"
        assert decision.evidence["facets"]["multilingual"]["evidenced_by"] == (
            "language_names"
        )
        assert len(scorer.calls) == 1

    def test_a_facet_evidenced_by_a_surface_form_says_so(self):
        decision = _one(
            _gate(), "Multilingual Prompt Injection Study", "Injected instructions."
        )

        facets = decision.evidence["facets"]
        assert facets["multilingual"]["evidenced_by"] == "surface_form"
        assert facets["prompt injection"]["evidenced_by"] == "surface_form"

    def test_an_unevidenced_facet_has_no_evidencing_rule(self):
        decision = _one(_gate(), "Multilingual Jailbreak Study", "Jailbreaks.")

        assert decision.evidence["facets"]["prompt injection"]["evidenced_by"] is None
