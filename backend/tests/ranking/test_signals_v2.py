"""Stage 8's two rewritten signals (D-08-10, D-08-11).

Both changes come out of the same corpus finding. On the 20-paper LLM-security
corpus every cluster scored `solution_density == 0.00`, including one whose own
members include Sentra-Guard — a paper whose contributions are a prompt
injection *defense* that "neutralized more than 98.7% of adversarial attempts".
It scored zero because the old rule asked whether a contribution sentence
contained every term of the label "prompt attacks injection" verbatim, and no
contribution sentence in the corpus contains all three.

At the same time one uncorroborated NLI false positive contributed the whole of
`w5` (5.0 of an 11.01 score). Both tests below are written against the real
sentences.
"""

import pytest

from app.ranking.signals import (
    contradiction_signal,
    index_contributions,
    solution_density,
)

# --- Sentra-Guard's real contribution sentences (paper 9) -------------------
SENTRA_GUARD = [
    (
        "Delivered as an open-source, backend-agnostic toolkit, Sentra-Guard "
        "achieves real-time defense capability with enterprise-grade "
        "reliability and operational transparency."
    ),
    (
        "Across all conditions, the framework neutralized more than 98.7% of "
        "adversarial attempts, including zero-day variants."
    ),
    (
        "Sentra-Guard is backend-agnostic and compatible with major LLM "
        "deployment ecosystems."
    ),
]

# MIPIAD's (paper 2): a defense that names the theme directly.
MIPIAD = [
    (
        "We present a multilingual indirect prompt injection attack detection "
        "pipeline that filters adversarial instructions before they reach the "
        "model."
    )
]

# A paper in the corpus that measures the problem and proposes nothing.
MEASUREMENT_ONLY = [
    (
        "The current study tests unique prompt injections against a diverse "
        "set of 36 LLMs."
    ),
    (
        "The results reveal that 28% of the tested LLMs (10 out of 36) were "
        "highly vulnerable, failing all four prompt injections."
    ),
]

LABEL = "prompt attacks injection"


def density(label, mentioning_paper_ids, contributions_by_paper, **kwargs):
    return solution_density(
        label,
        mentioning_paper_ids,
        index_contributions(contributions_by_paper),
        **kwargs,
    )


class TestSolutionDensityRecognizesDifferentWording:
    def test_sentra_guard_counts_as_a_solver_of_the_theme_it_belongs_to(self) -> None:
        # The regression: paper 9 is a member of the cluster (it contributed a
        # limitation) and its contributions are a defense, but it shares not one
        # word with the label "prompt attacks injection".
        assert density(LABEL, {5, 9}, {9: SENTRA_GUARD}) == pytest.approx(0.5)

    def test_the_old_all_label_terms_rule_would_have_scored_zero(self) -> None:
        # Pinning the exact reason the signal was dead: no Sentra-Guard
        # contribution contains all three label terms.
        label_terms = {"prompt", "attacks", "injection"}
        assert not any(
            label_terms <= set(sentence.lower().split()) for sentence in SENTRA_GUARD
        )

    def test_a_paper_that_only_measures_the_problem_is_not_a_solver(self) -> None:
        # It names the theme in every sentence but proposes no remedy, so it is
        # evidence *of* the gap, not evidence the gap is being closed.
        assert density(LABEL, {5, 9}, {5: MEASUREMENT_ONLY}) == 0.0

    def test_a_solver_outside_the_cluster_is_found_through_the_label(self) -> None:
        # D-08-04 is preserved: a paper that solved a problem rarely lists it as
        # its own limitation, so it is not a cluster member. MIPIAD is linked by
        # sharing label terms instead.
        assert density(LABEL, {5, 10}, {2: MIPIAD}) == pytest.approx(0.5)

    def test_an_unrelated_defense_paper_is_not_counted(self) -> None:
        unrelated = [
            (
                "We propose a differential privacy defense that protects "
                "training records in federated medical imaging."
            )
        ]
        assert density(LABEL, {5, 10}, {77: unrelated}) == 0.0

    def test_solvers_are_counted_once_per_paper(self) -> None:
        assert density(LABEL, {5, 9}, {9: SENTRA_GUARD * 4}) == pytest.approx(0.5)

    def test_density_is_clamped_to_one(self) -> None:
        many = {pid: MIPIAD for pid in range(20, 30)}
        assert density(LABEL, {5}, many) == 1.0

    def test_label_overlap_requirement_is_configurable(self) -> None:
        assert density(LABEL, {5}, {2: MIPIAD}, min_label_overlap=2) == 1.0
        assert density(LABEL, {5}, {2: MIPIAD}, min_label_overlap=99) == 0.0

    def test_membership_links_a_paper_even_without_a_label(self) -> None:
        # A cluster with no label still has papers, and they are what the theme
        # is. Previously an unlabelled cluster scored a flat zero.
        assert density(None, {9}, {9: SENTRA_GUARD}) == 1.0

    def test_no_mentioning_papers_is_zero_not_a_crash(self) -> None:
        assert density(LABEL, set(), {9: SENTRA_GUARD}) == 0.0

    def test_no_contributions_at_all_is_zero(self) -> None:
        assert density(LABEL, {5, 9}, {}) == 0.0


class TestGradedContradictionSignal:
    def test_no_disagreement_scores_zero(self) -> None:
        assert contradiction_signal(0, min_paper_pairs=2, full_strength=3) == 0.0

    def test_below_corroboration_scores_zero(self) -> None:
        # One paper pair is what cluster 1 really had, and it contributed the
        # whole of w5 under the old boolean.
        assert contradiction_signal(1, min_paper_pairs=2, full_strength=3) == 0.0

    def test_a_corroborated_disagreement_is_graded_not_full(self) -> None:
        # The fix D-08-10 asks for: two disagreeing paper pairs is real evidence
        # but not the strongest a cluster can show, so it earns two thirds of w5.
        assert contradiction_signal(
            2, min_paper_pairs=2, full_strength=3
        ) == pytest.approx(2 / 3)

    def test_full_strength_is_reached_and_clamped(self) -> None:
        assert contradiction_signal(3, min_paper_pairs=2, full_strength=3) == 1.0
        assert contradiction_signal(9, min_paper_pairs=2, full_strength=3) == 1.0

    def test_the_signal_never_leaves_zero_one(self) -> None:
        for pairs in range(12):
            value = contradiction_signal(pairs, min_paper_pairs=2, full_strength=3)
            assert 0.0 <= value <= 1.0

    def test_corroboration_can_be_relaxed_to_a_single_pair(self) -> None:
        assert contradiction_signal(
            1, min_paper_pairs=1, full_strength=3
        ) == pytest.approx(1 / 3)

    def test_a_negative_count_is_treated_as_none(self) -> None:
        assert contradiction_signal(-1, min_paper_pairs=1, full_strength=3) == 0.0
