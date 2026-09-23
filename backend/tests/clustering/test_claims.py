"""Stage 6's pre-NLI gates (D-06-10, D-06-11).

The corpus that motivated these gates is on record: cluster 1 of the 20-paper
LLM-security corpus was flagged "Conflicting evidence" on four pairs that share
one right-hand statement — an ethics/availability disclaimer — against a
recommendation bullet, a related-work description, a future-work proposal and
one genuine limitation. None of the four is a disagreement. Every one of those
exact statements appears below.
"""

import pytest

from app.clustering.claims import (
    STOPWORDS,
    claim_rejection,
    content_terms,
    is_claim_like,
    topic_overlap,
    topically_related,
)

# --- the five real statements behind cluster 1's false positives -------------

DISCLAIMER = (
    "All attack templates are derived from the publicly available BIPIA "
    "benchmark (Yi et al., 2023) and are used solely to evaluate detection "
    "methods; no attack tooling is released."
)
RECOMMENDATION = (
    "• Utilize AI telemetry logging for monitoring inputs and outputs to "
    "detect and mitigate security threats."
)
RELATED_WORK = (
    "These studies systematically assess attack success rates (ASR) as a "
    "standardized metric to quantify how frequently models bypass safety "
    "filters."
)
PROPOSAL = (
    "Another area for investigation is multi-step prompt injection attacks, "
    "where LLMs are tested for their susceptibility to complex, multi-step "
    "command sequences."
)
GENUINE_LIMITATION = (
    "The variation in vulnerabilities across different prompt designs "
    "underscores the limitations of current defenses and the need for "
    "adaptive safeguards capable of addressing a range of attack vectors"
)


class TestIsClaimLike:
    @pytest.mark.parametrize(
        ("text", "reason"),
        [
            (RECOMMENDATION, "bullet"),
            (DISCLAIMER, "disclaimer"),
            (RELATED_WORK, "related_work"),
            (PROPOSAL, "proposal"),
        ],
        ids=["recommendation", "disclaimer", "related-work", "future-work"],
    )
    def test_cluster_1_false_positive_statements_are_not_claims(
        self, text: str, reason: str
    ) -> None:
        assert is_claim_like(text) is False
        assert claim_rejection(text) == reason

    def test_a_real_limitation_is_a_claim(self) -> None:
        assert is_claim_like(GENUINE_LIMITATION) is True
        assert claim_rejection(GENUINE_LIMITATION) is None

    def test_a_finding_that_opposes_another_is_a_claim(self) -> None:
        assert is_claim_like(
            "Across all conditions, the framework neutralized more than 98.7% "
            "of adversarial prompt injection attempts, including zero-day "
            "variants."
        )

    @pytest.mark.parametrize(
        ("text", "reason"),
        [
            ("DISCUSSION AND LIMITATIONS", "too_short"),
            ("This study has several limitations.", "too_short"),
            ("3.1.3", "bullet"),
            ("", "empty"),
            ("   ", "empty"),
            (
                (
                    "Section VII provides a discussion of key findings "
                    "and limitations of the evaluated systems."
                ),
                "structural",
            ),
            (
                (
                    "Continuously monitor LLM inputs and outputs for "
                    "anomalies to detect potential security breaches."
                ),
                "imperative",
            ),
            (
                (
                    "Any content generated does not reflect the view of "
                    "the authors of this paper."
                ),
                "disclaimer",
            ),
        ],
        ids=[
            "heading",
            "boilerplate",
            "section-number-enumerator",
            "empty",
            "whitespace",
            "structural",
            "adverb-led-imperative",
            "ethics-disclaimer",
        ],
    )
    def test_boilerplate_and_metadata_are_not_claims(
        self, text: str, reason: str
    ) -> None:
        assert is_claim_like(text) is False
        assert claim_rejection(text) == reason

    def test_a_verb_outside_any_closed_list_still_reads_as_a_claim(self) -> None:
        # Regression: an earlier finite-verb rule rejected this project's own
        # hand-labelled genuine contradiction because "recovers" was not on
        # the list. No closed verb list gates a claim any more.
        assert is_claim_like(
            "Cross-lingual transfer fully recovers accuracy for "
            "low-resource languages."
        )

    def test_none_is_not_a_claim(self) -> None:
        assert is_claim_like(None) is False


class TestContentTerms:
    def test_stopwords_are_dropped(self) -> None:
        assert content_terms("The models are evaluated") == {"model", "evaluat"}

    def test_terms_are_stemmed_so_wording_differences_still_match(self) -> None:
        assert content_terms("injection attacks") == content_terms("injections attack")

    def test_short_tokens_and_punctuation_are_dropped(self) -> None:
        assert content_terms("a b c -- 42 %") == set()

    def test_empty_text_has_no_terms(self) -> None:
        assert content_terms(None) == set()

    def test_stopwords_are_exposed_for_reuse(self) -> None:
        assert "the" in STOPWORDS


class TestTopicallyRelated:
    def test_the_disclaimer_is_unrelated_to_every_statement_it_was_paired_with(
        self,
    ) -> None:
        for other in (RECOMMENDATION, RELATED_WORK, PROPOSAL, GENUINE_LIMITATION):
            shared, jaccard = topic_overlap(DISCLAIMER, other)
            assert shared <= 1, (shared, other)
            assert jaccard < 0.05, (jaccard, other)
            assert topically_related(DISCLAIMER, other) is False

    def test_two_statements_about_the_same_thing_are_related(self) -> None:
        assert topically_related(
            "Current prompt injection defenses fail to generalize and leave "
            "models vulnerable to adaptive attacks.",
            "Our prompt injection defense generalizes to adaptive attacks and "
            "leaves models protected in every tested configuration.",
        )

    def test_relatedness_is_symmetric(self) -> None:
        left = "Prompt injection defenses fail against adaptive attacks."
        right = "Prompt injection defenses succeed against adaptive attacks."
        assert topic_overlap(left, right) == topic_overlap(right, left)

    def test_thresholds_are_injectable(self) -> None:
        left, right = "alpha beta gamma delta", "alpha beta gamma epsilon"
        assert topically_related(left, right, min_shared=3, min_jaccard=0.1)
        assert not topically_related(left, right, min_shared=4, min_jaccard=0.1)
        assert not topically_related(left, right, min_shared=3, min_jaccard=0.9)
