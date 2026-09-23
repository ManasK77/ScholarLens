"""The S6 clustering input filter (D-06-13).

A read-only audit of the 20-paper LLM-security corpus found that roughly 11 of
27 stored clusters were not themes at all. They were document furniture that
Stage 2/3 had extracted as `limitation` statements and Stage 5 had embedded:
reference-list entries, bibliography author lists, citation year fragments,
acknowledgements and funding lines, section cross-references and headings.

Four of them reached the projected top 10, and they ranked *well* — a cluster
of author names has no on-theme solver, so its `solution_density` is 0.00 and
nothing is subtracted from its score. The ranking was rewarding extraction
noise.

Every rejected string in this module is copied verbatim from that corpus.

**This is not `claim_rejection`.** That predicate answers "could another paper
contradict this?" and correctly rejects proposals, so reusing it here would
throw away the future-work statements README §8 Stage 6 requires this phase to
cluster. `research_statement_rejection` answers a weaker question — "is this a
research statement at all, or is it document furniture?" — and the tests in
`TestLegitimateStatementsSurvive` are what hold that line.
"""

import pytest

from app.clustering.claims import (
    MIN_RESEARCH_WORDS,
    is_research_statement,
    research_statement_rejection,
)

# --- verbatim noise from the corpus -----------------------------------------

BIBLIOGRAPHY = [
    (
        "In Findings of the Association for Computational Linguistics: EMNLP 2023, "
        "pages 12365-12394, Singapore."
    ),
    (
        "Proceedings of the 2020 Conference on Empirical Methods in Natural "
        "Language Processing (EMNLP), pages 5943-5959, Online."
    ),
    (
        "In Proceedings of the 61st Annual Meeting of the Association for "
        "Computational Linguistics, pages 1-18, Toronto, Canada."
    ),
]

AUTHOR_LISTS = [
    (
        "Zhengbao Jiang, Antonios Anastasopoulos, Jun Araki, Haibo Ding, and "
        "Graham Neubig."
    ),
    "Claire Cui, Olivier Bousquet, Quoc V. Le, and Ed H. Chi.",
    (
        "Takeshi Kojima, Shixiang Shane Gu, Machel Reid, Yutaka Matsuo, and "
        "Yusuke Iwasawa."
    ),
]

ACKNOWLEDGEMENTS = [
    (
        "He has received many notable awards and fellowships including the "
        "Facebook ethics for AI research award, India, Google course award for the "
        "course AI for everyone."
    ),
    (
        "(MAPG) and the PaliGemma Academic Program award from Google for her work "
        "in AI safety alignment."
    ),
    (
        "We thank the anonymous reviewers for their detailed and constructive "
        "comments on an earlier draft of this paper."
    ),
    (
        "This research was supported in part by a grant from the National Science "
        "Foundation under award number 1234567."
    ),
]

CITATION_FRAGMENTS = ["2023.", "2022.", "(Ghosal et al., CVPR 2025)", "3.1.3"]

HEADINGS_AND_BOILERPLATE = [
    "DISCUSSION AND LIMITATIONS",
    "This study has several limitations.",
    "Association for Computational Linguistics.",
    "Section VII provides a discussion of key findings and limitations.",
    "Any content generated does not reflect the view of the authors.",
    "RESULTS AND DISCUSSION OF THE MULTILINGUAL EVALUATION EXPERIMENTS",
]

# --- verbatim research statements that must survive -------------------------

LIMITATIONS = [
    (
        "The variation in vulnerabilities across different prompt designs "
        "underscores the limitations of current defenses and the need for "
        "adaptive safeguards capable of addressing a range of attack vectors"
    ),
    (
        "Overall, 56% of all 144 tests resulted in successful prompt injections, "
        "while 44% of the models resisted."
    ),
    (
        "Our evaluation is limited to English, and accuracy degrades sharply on "
        "low-resource languages."
    ),
    (
        "The test set in this work uses an attack-to-benign ratio of 10:1, which "
        "is far higher than real deployments."
    ),
    (
        "When models are explicitly instructed to think in the same language as "
        "the prompt language, many fail to follow the instruction."
    ),
]

FUTURE_WORK = [
    (
        "Future work includes extending this analysis to additional conferences "
        "and LLMs, exploring diverse injection strategies and placements, and "
        "investigating mitigation techniques to improve robustness against "
        "document-based multilingual prompt injection attacks."
    ),
    (
        "Another area for investigation is multi-step prompt injection attacks, "
        "where LLMs are tested for their susceptibility to complex, multi-step "
        "command sequences."
    ),
    (
        "Future research could apply mechanistic interpretability methods to "
        "investigate model internals to better understand the sources of "
        "multilingual inconsistency."
    ),
]

# Contribution / dataset / metric / model claims, which Stage 3 also extracts.
OTHER_RESEARCH_CLAIMS = [
    (
        "Across all conditions, the framework neutralized more than 98.7% of "
        "adversarial attempts, including zero-day variants."
    ),
    (
        "We release a multilingual benchmark of 12,000 annotated prompts covering "
        "fourteen typologically diverse languages."
    ),
    (
        "We evaluate GPT-4o, Gemini 1.5-Pro, Qwen-Max, and DeepSeek-R1 under four "
        "distinct attack conditions."
    ),
    (
        "Attack success rate is reported as the median across six harmful content "
        "categories rather than the mean."
    ),
    (
        "English, Spanish, French, and German were evaluated with the same "
        "prompt templates throughout."
    ),
]


class TestBibliographyIsRejected:
    @pytest.mark.parametrize("text", BIBLIOGRAPHY)
    def test_reference_entries_are_rejected(self, text: str) -> None:
        assert research_statement_rejection(text) == "bibliography"
        assert is_research_statement(text) is False


class TestAuthorListsAreRejected:
    @pytest.mark.parametrize("text", AUTHOR_LISTS)
    def test_bibliography_author_lists_are_rejected(self, text: str) -> None:
        assert research_statement_rejection(text) == "author_list"
        assert is_research_statement(text) is False

    def test_a_sentence_that_merely_lists_systems_is_not_an_author_list(self) -> None:
        # The risk this rule runs: capitalized comma-separated items that are
        # models, not people. The verb in the first segment is what separates
        # them.
        assert is_research_statement(
            "We evaluate GPT-4o, Gemini 1.5-Pro, Qwen-Max, and DeepSeek-R1 "
            "under four distinct attack conditions."
        )


class TestAcknowledgementsAreRejected:
    @pytest.mark.parametrize("text", ACKNOWLEDGEMENTS)
    def test_funding_and_thanks_are_rejected(self, text: str) -> None:
        assert research_statement_rejection(text) == "acknowledgement"
        assert is_research_statement(text) is False

    def test_supported_by_in_ordinary_prose_is_not_an_acknowledgement(self) -> None:
        # Regression: an earlier pattern matched bare "supported by" and threw
        # away this project's own hand-labelled hallucination limitation.
        assert is_research_statement(
            "The system still hallucinates factual claims that are not "
            "supported by the retrieved context."
        )
        assert is_research_statement(
            "This finding is supported by the ablation in Table 4 and holds "
            "across every model we tested."
        )

    def test_award_as_a_verb_is_not_an_acknowledgement(self) -> None:
        # "awards" needs funding context, so an ML sentence using it as a verb
        # survives. ("reward" never matches: the pattern is word-bounded.)
        assert is_research_statement(
            "The scoring function awards higher probability mass to sequences "
            "that terminate early, which biases the decoder."
        )


class TestCitationFragmentsAreRejected:
    @pytest.mark.parametrize("text", CITATION_FRAGMENTS)
    def test_year_and_citation_fragments_are_rejected(self, text: str) -> None:
        assert is_research_statement(text) is False

    def test_a_bare_inline_citation_is_rejected(self) -> None:
        assert research_statement_rejection("(Ghosal et al., CVPR 2025)") == "too_short"


class TestHeadingsAndBoilerplateAreRejected:
    @pytest.mark.parametrize("text", HEADINGS_AND_BOILERPLATE)
    def test_headings_and_boilerplate_are_rejected(self, text: str) -> None:
        assert is_research_statement(text) is False

    def test_an_all_caps_heading_is_rejected_even_when_long(self) -> None:
        assert (
            research_statement_rejection(
                "RESULTS AND DISCUSSION OF THE MULTILINGUAL EVALUATION EXPERIMENTS"
            )
            == "heading"
        )

    def test_an_acronym_inside_a_sentence_does_not_make_it_a_heading(self) -> None:
        assert is_research_statement(
            "The ASR reported for GPT-4o under the AS4 condition is lower than "
            "the ASR reported for the full attack."
        )


class TestLegitimateStatementsSurvive:
    @pytest.mark.parametrize("text", LIMITATIONS)
    def test_real_limitations_are_kept(self, text: str) -> None:
        assert is_research_statement(text), research_statement_rejection(text)

    @pytest.mark.parametrize("text", FUTURE_WORK)
    def test_real_future_work_is_kept(self, text: str) -> None:
        # The reason this filter is not `claim_rejection`: that predicate
        # rejects every one of these as a "proposal", and README S8 Stage 6
        # clusters limitation *and future-work* statements.
        assert is_research_statement(text), research_statement_rejection(text)

    @pytest.mark.parametrize("text", OTHER_RESEARCH_CLAIMS)
    def test_contribution_dataset_metric_and_model_claims_are_kept(
        self, text: str
    ) -> None:
        assert is_research_statement(text), research_statement_rejection(text)


class TestOverRejectionGuards:
    """Cases the first draft of this filter wrongly threw away.

    All three came from reusing the contradiction filter's `_STRUCTURAL` and
    `_DISCLAIMER` patterns wholesale. They are right for "could another paper
    contradict this?" and wrong for "is this research?", so the clustering
    boundary uses narrower ones (D-06-13).
    """

    @pytest.mark.parametrize(
        "text",
        [
            "Table 4 shows that accuracy drops sharply for low-resource languages.",
            "Figure 3 illustrates the failure mode we observed across all six models.",
            (
                "Section 5 limitations include our reliance on a single "
                "annotator per example."
            ),
        ],
    )
    def test_a_finding_that_opens_with_a_float_reference_is_kept(self, text) -> None:
        # The cross-reference rule needs the structural verb, not just the
        # opener: "Section VII *provides a discussion*" is apparatus, "Table 4
        # *shows that accuracy drops*" is a finding.
        assert is_research_statement(text), research_statement_rejection(text)

    @pytest.mark.parametrize(
        "text",
        [
            "The ethical implications of deploying these models remain underexplored.",
            (
                "Ethical risks of multilingual jailbreaks are not addressed by "
                "current benchmarks."
            ),
        ],
    )
    def test_ethics_as_a_research_topic_is_kept(self, text) -> None:
        # In an AI-safety corpus this is a research gap, not an ethics
        # statement. Only unambiguous conduct boilerplate (consent, IRB, "does
        # not reflect the view of the authors") is furniture.
        assert is_research_statement(text), research_statement_rejection(text)

    def test_a_dataset_availability_claim_is_kept(self) -> None:
        assert is_research_statement(
            "Our dataset is publicly available and covers fourteen languages."
        )

    def test_a_mid_sentence_venue_mention_is_kept(self) -> None:
        # The bibliography rule anchors "Proceedings of the" at the start, so a
        # real sentence that names a venue survives.
        assert is_research_statement(
            "We evaluate on Proceedings of the ACL corpus subset."
        )

    def test_the_real_cross_reference_is_still_rejected(self) -> None:
        assert (
            research_statement_rejection(
                "Section VII provides a discussion of key findings and limitations."
            )
            == "structural"
        )

    def test_the_real_conduct_disclaimer_is_still_rejected(self) -> None:
        assert (
            research_statement_rejection(
                "Any content generated does not reflect the view of the authors."
            )
            == "disclaimer"
        )


class TestContract:
    def test_empty_and_missing_text_is_rejected(self) -> None:
        assert research_statement_rejection(None) == "empty"
        assert research_statement_rejection("   ") == "empty"

    def test_the_word_floor_is_injectable(self) -> None:
        short = "Multilingual coverage remains poor."

        assert research_statement_rejection(short, min_words=3) is None
        assert research_statement_rejection(short, min_words=20) == "too_short"

    def test_the_default_floor_is_lower_than_the_contradiction_floor(self) -> None:
        # Contradiction needs a full assertable claim; clustering only needs a
        # statement with a theme in it, so this floor is deliberately looser.
        from app.clustering.claims import MIN_CLAIM_WORDS

        assert MIN_RESEARCH_WORDS < MIN_CLAIM_WORDS

    def test_the_verdict_is_deterministic(self) -> None:
        text = BIBLIOGRAPHY[0]

        assert research_statement_rejection(text) == research_statement_rejection(text)
