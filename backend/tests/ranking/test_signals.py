"""Phase 8 — every gap-score signal, tested on its own before the combination.

README §15 Phase 8: *"test each signal term independently, then the combined
score"*. This module is pure — plain values in, plain numbers out — so each
term's definition is pinned without a database, a cluster, or a weight in
sight. `test_service.py` covers the wiring; `test_ranking.py` the combination.
"""

import math

import pytest

from app.ranking.signals import (
    GapSignals,
    citation_impact,
    contradiction_signal,
    gap_score,
    index_contributions,
    mention_frequency,
    solution_density,
    trend_slope_signal,
)


def _density(label, mentioning_papers, contributions_by_paper) -> float:
    """`solution_density` over raw contribution text.

    The production caller prepares the corpus once (`index_contributions`);
    these tests read better against the statements themselves, so they go
    through the same function the service does.

    `mentioning_papers` is an int here for readability. The signal takes the
    cluster's actual paper ids (a paper that is a member is on-theme by
    membership, D-08-11), so a count is expanded into ids that deliberately do
    not collide with the contributing papers' ids -- these cases are about
    label matching, and membership is covered in test_signals_v2.py.
    """
    return solution_density(
        label,
        range(9000, 9000 + mentioning_papers),
        index_contributions(contributions_by_paper),
    )


# ---------------------------------------------------------------------------
# mention_frequency — distinct papers, not statements (D-08-02)
# ---------------------------------------------------------------------------


def test_mention_frequency_counts_papers_not_statements() -> None:
    """One paper contributing four limitation sentences mentioned it once."""
    assert mention_frequency([7, 7, 7, 7]) == 1


def test_mention_frequency_counts_each_distinct_paper() -> None:
    assert mention_frequency([1, 2, 3, 2, 1]) == 3


def test_mention_frequency_of_an_empty_cluster_is_zero() -> None:
    assert mention_frequency([]) == 0


# ---------------------------------------------------------------------------
# trend_slope — Phase 7's stored value, signed and unscaled (D-08-03)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slope", [2.5, 0.0, -1.75])
def test_trend_slope_is_passed_through_signed(slope: float) -> None:
    """A declining theme must be able to lower a score, not just fail to raise it."""
    assert trend_slope_signal(slope) == slope


def test_a_missing_trend_scores_zero() -> None:
    """Defensive only — D-07-07 guarantees one trend row per cluster."""
    assert trend_slope_signal(None) == 0.0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_slope_is_refused_rather_than_propagated(value: float) -> None:
    """A NaN would make every comparison in the ranking false, silently."""
    assert trend_slope_signal(value) == 0.0


# ---------------------------------------------------------------------------
# solution_density — contributions matched against the cluster label (D-08-04)
# ---------------------------------------------------------------------------


def test_solution_density_is_solvers_over_mentioning_papers() -> None:
    """ "papers proposing solutions / papers mentioning the problem" (README §8)."""
    density = _density(
        label="multilingual injection prompt",
        mentioning_papers=4,
        contributions_by_paper={
            1: ["We propose a multilingual prompt injection defense."],
            2: ["We contribute a faster attention kernel."],
        },
    )

    assert density == 0.25


def test_one_shared_label_term_is_not_enough() -> None:
    """A single shared term would match half the corpus of a focused survey.

    D-08-11 relaxed the old every-term rule to an overlap of two, which is what
    keeps a shared word like "multilingual" from marking the field as solved
    while still recognising a differently-worded defense.
    """
    density = _density(
        label="multilingual injection prompt",
        mentioning_papers=1,
        contributions_by_paper={1: ["We propose multilingual defenses."]},
    )

    assert density == 0.0


def test_a_contribution_that_names_the_theme_but_proposes_nothing_is_not_a_solver() -> (
    None
):
    """Naming the problem is evidence *of* the gap, not evidence it is closed."""
    density = _density(
        label="multilingual injection prompt",
        mentioning_papers=1,
        contributions_by_paper={
            1: ["We measure multilingual prompt injection success rates."]
        },
    )

    assert density == 0.0


def test_label_terms_are_matched_case_insensitively() -> None:
    density = _density(
        label="multilingual injection",
        mentioning_papers=1,
        contributions_by_paper={1: ["Multilingual INJECTION defences are proposed."]},
    )

    assert density == 1.0


def test_label_terms_are_matched_after_stemming() -> None:
    """D-08-11: "injections" and "injection" are the same term.

    The old rule compared raw words, so a plural in the contribution and a
    singular in the frequency-derived label never matched.
    """
    density = _density(
        label="multilingual injection",
        mentioning_papers=1,
        contributions_by_paper={
            1: ["Multilingual injections are mitigated by our filter."]
        },
    )

    assert density == 1.0


def test_label_terms_are_matched_on_word_boundaries() -> None:
    """ "injection" must not be found inside "injectionless"."""
    density = _density(
        label="injection defense",
        mentioning_papers=1,
        contributions_by_paper={
            1: ["An injectionless reinjectionary defense approach."]
        },
    )

    assert density == 0.0


def test_a_paper_with_several_matching_contributions_still_counts_once() -> None:
    density = _density(
        label="hallucination mitigation",
        mentioning_papers=2,
        contributions_by_paper={
            1: [
                "We mitigate hallucination.",
                "We measure hallucination mitigation.",
            ]
        },
    )

    assert density == 0.5


def test_solution_density_is_clamped_to_one() -> None:
    """The numerator is corpus-wide, so it can exceed the mentioning papers.

    Clamping keeps the `- w3 * density` term bounded and keeps the value
    readable as a density (D-08-04).
    """
    density = _density(
        label="hallucination mitigation",
        mentioning_papers=1,
        contributions_by_paper={
            1: ["Hallucination mitigation is improved by our defense."],
            2: ["We detect hallucination with a mitigation filter."],
            3: ["Our hallucination mitigation hardens the decoder."],
        },
    )

    assert density == 1.0


def test_an_unlabelled_cluster_matches_nothing_through_its_label() -> None:
    """`Cluster.label` is nullable by Phase 6 contract.

    With no label there is no theme to match a *non-member* paper against, so
    the label route contributes nothing. Its own papers still count by
    membership -- see test_signals_v2.py.
    """
    assert (
        _density(
            label=None,
            mentioning_papers=3,
            contributions_by_paper={1: ["Our defense mitigates the problem."]},
        )
        == 0.0
    )


def test_a_label_of_only_punctuation_has_no_solution_density() -> None:
    assert (
        _density(
            label="  --  ",
            mentioning_papers=3,
            contributions_by_paper={1: ["Our defense mitigates --."]},
        )
        == 0.0
    )


def test_solution_density_of_a_cluster_with_no_papers_is_zero() -> None:
    """Never a ZeroDivisionError: a cluster with no papers has no density."""
    assert (
        _density(
            label="hallucination mitigation",
            mentioning_papers=0,
            contributions_by_paper={1: ["We mitigate hallucination."]},
        )
        == 0.0
    )


def test_blank_contributions_never_match() -> None:
    assert (
        _density(
            label="hallucination",
            mentioning_papers=2,
            contributions_by_paper={1: ["", "   "]},
        )
        == 0.0
    )


# ---------------------------------------------------------------------------
# citation_impact — mean citations of the mentioning papers (D-08-05)
# ---------------------------------------------------------------------------


def test_citation_impact_is_the_mean_not_the_sum() -> None:
    """A sum would just be a second copy of mention_frequency."""
    assert citation_impact([10, 20, 30]) == 20.0


def test_an_unknown_citation_count_is_treated_as_zero() -> None:
    """An arXiv record never matched to Semantic Scholar has no count.

    Dropping unknowns instead would let one known paper set a whole theme's
    impact; unknown means no evidence of impact.
    """
    assert citation_impact([10, None, None, None]) == 2.5


def test_citation_impact_of_a_cluster_with_no_papers_is_zero() -> None:
    assert citation_impact([]) == 0.0


def test_a_negative_citation_count_is_floored_at_zero() -> None:
    """Metadata is external; a negative count is corrupt, not a penalty."""
    assert citation_impact([-5, 10]) == 5.0


# ---------------------------------------------------------------------------
# contradiction_flag — the flag, at 0.0/1.0 (D-08-06)
# ---------------------------------------------------------------------------


def test_contradiction_signal_is_one_at_full_strength() -> None:
    # D-08-10: graded on distinct disagreeing paper pairs, not a boolean. The
    # grading itself is covered in test_signals_v2.py.
    assert contradiction_signal(3, min_paper_pairs=2, full_strength=3) == 1.0


def test_contradiction_signal_is_zero_when_not_flagged() -> None:
    assert contradiction_signal(0, min_paper_pairs=2, full_strength=3) == 0.0


# ---------------------------------------------------------------------------
# gap_score — the combination (GAPRANK-01)
# ---------------------------------------------------------------------------

WEIGHTS = (1.0, 3.0, 25.0, 0.02, 5.0)


def _signals(**overrides) -> GapSignals:
    values = {
        "mention_frequency": 0,
        "trend_slope": 0.0,
        "solution_density": 0.0,
        "citation_impact": 0.0,
        "contradiction_flag": 0.0,
    }
    values.update(overrides)
    return GapSignals(**values)


def test_the_formula_is_the_readme_formula() -> None:
    """w1*freq + w2*slope - w3*density + w4*citations + w5*flag."""
    signals = _signals(
        mention_frequency=27,
        trend_slope=2.5,
        solution_density=0.2,
        citation_impact=150.0,
        contradiction_flag=1.0,
    )

    expected = 1.0 * 27 + 3.0 * 2.5 - 25.0 * 0.2 + 0.02 * 150.0 + 5.0 * 1.0

    assert gap_score(signals, WEIGHTS) == pytest.approx(expected)


def test_solution_density_is_subtracted_not_added() -> None:
    """The sign of this one term is the difference between a gap and a solved problem."""
    solved = gap_score(_signals(mention_frequency=10, solution_density=1.0), WEIGHTS)
    unsolved = gap_score(_signals(mention_frequency=10, solution_density=0.0), WEIGHTS)

    assert solved < unsolved


def test_a_declining_theme_scores_below_an_identical_rising_one() -> None:
    rising = gap_score(_signals(mention_frequency=10, trend_slope=2.0), WEIGHTS)
    falling = gap_score(_signals(mention_frequency=10, trend_slope=-2.0), WEIGHTS)

    assert falling < rising


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mention_frequency", 10),
        ("trend_slope", 1.0),
        ("citation_impact", 100.0),
        ("contradiction_flag", 1.0),
    ],
)
def test_every_positive_signal_raises_the_score(field: str, value: float) -> None:
    assert gap_score(_signals(**{field: value}), WEIGHTS) > gap_score(
        _signals(), WEIGHTS
    )


def test_a_zero_weight_removes_a_signal_entirely() -> None:
    signals = _signals(mention_frequency=40, contradiction_flag=1.0)

    assert gap_score(signals, (0.0, 3.0, 25.0, 0.02, 5.0)) == 5.0


def test_an_empty_cluster_scores_exactly_zero() -> None:
    assert gap_score(_signals(), WEIGHTS) == 0.0


def test_the_score_is_rounded_for_reproducibility() -> None:
    """D-08-08: a float tail below any weight's significance, dropped."""
    score = gap_score(_signals(citation_impact=1 / 3), (0.0, 0.0, 0.0, 1.0, 0.0))

    assert score == 0.333333


def test_negative_zero_is_normalized() -> None:
    """`-0.0` renders as "-0.00" in the dashboard's score pill."""
    score = gap_score(_signals(solution_density=0.0), WEIGHTS)

    assert math.copysign(1.0, score) == 1.0


def test_wrong_weight_count_is_refused() -> None:
    """The weights are positional; a short tuple would silently misalign them."""
    with pytest.raises(ValueError, match="five weights"):
        gap_score(_signals(), (1.0, 2.0))


def test_index_contributions_keeps_one_term_set_per_statement() -> None:
    indexed = index_contributions(
        {1: ["Our defense mitigates prompt injection.", "A faster kernel."]}
    )

    assert len(indexed[1].terms) == 2
    assert "inject" in indexed[1].terms[0]
    assert indexed[1].proposes_solution is True


def test_index_contributions_marks_a_paper_with_no_remedy() -> None:
    indexed = index_contributions({1: ["We measure prompt injection success."]})

    assert indexed[1].proposes_solution is False


def test_index_contributions_of_an_empty_corpus_is_empty() -> None:
    assert index_contributions({}) == {}
