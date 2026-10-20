"""Threshold policy comparison: tallies by type, floor fitting, cross-fitting, verdict."""

import pytest
from evaluation import threshold_policy as tp

LABELS = {
    "a": {"label": "relevant", "question_type": "factual"},
    "b": {"label": "relevant", "question_type": "numeric"},
    "c": {"label": "partially_relevant", "question_type": "factual"},
    "d": {"label": "weak", "question_type": "adjacent_topic"},
    "e": {"label": "unrelated", "question_type": "off_topic"},
    "f": {"label": "unrelated", "question_type": "off_topic"},
}


def test_tally_counts_accepted_per_label_and_type():
    decisions = {"a": True, "b": False, "c": True, "d": True, "e": False, "f": False}

    by_label = tp.tally(list(LABELS), LABELS, decisions)
    by_type = tp.tally(list(LABELS), LABELS, decisions, group="question_type")

    assert by_label["relevant"] == {"accepted": 1, "n": 2}
    assert by_label["unrelated"] == {"accepted": 0, "n": 2}
    assert by_type["factual"]["relevant"] == {"accepted": 1, "n": 1}
    assert by_type["factual"]["partially_relevant"] == {"accepted": 1, "n": 1}
    # A label a group never contains is absent, not a 0/0 row.
    assert "unrelated" not in by_type["factual"]


def test_answerable_split_groups_relevant_and_partial_against_the_rest():
    decisions = {"a": True, "b": True, "c": False, "d": True, "e": False, "f": False}

    split = tp.answerable_split(list(LABELS), LABELS, decisions)

    assert split["answerable"] == {"accepted": 2, "n": 3}
    assert split["unanswerable"] == {"accepted": 1, "n": 3}


def test_rerank_top1_is_the_highest_logit_per_question():
    assert tp.rerank_top1({"a": [-3.0, 2.5, 0.1], "b": [-9.0]}) == {"a": 2.5, "b": -9.0}


def test_fit_floor_is_the_midpoint_between_worst_unrelated_and_worst_relevant():
    top1 = {"a": -2.0, "b": -4.0, "c": -3.0, "d": 1.0, "e": -8.0, "f": -10.0}

    assert tp.fit_floor(["a", "b", "e", "f"], LABELS, top1) == pytest.approx(-6.0)


def test_fit_floor_returns_none_when_the_classes_overlap():
    top1 = {"a": -9.0, "b": -4.0, "e": -8.0, "f": -10.0}

    assert tp.fit_floor(["a", "b", "e", "f"], LABELS, top1) is None


def test_cross_fit_fits_on_one_half_and_tests_on_the_other_in_both_directions():
    top1 = {"a": -2.0, "b": -4.0, "c": -3.0, "d": 1.0, "e": -8.0, "f": -10.0}
    folds = (["a", "e", "c"], ["b", "d", "f"])

    runs = tp.cross_fit(LABELS, top1, folds)

    assert [r["fit_fold"] for r in runs] == [0, 1]
    assert runs[0]["floor"] == pytest.approx(-5.0)  # midpoint of -8 and -2
    assert runs[1]["floor"] == pytest.approx(-7.0)  # midpoint of -10 and -4
    # Fold 0's floor is applied to fold 1: b (-4) accepted, f (-10) rejected.
    assert runs[0]["test"]["relevant"] == {"accepted": 1, "n": 1}
    assert runs[0]["test"]["unrelated"] == {"accepted": 0, "n": 1}
    assert runs[0]["answerable_rejected"] == []


def test_cross_fit_names_every_answerable_question_the_floor_rejects():
    top1 = {"a": -2.0, "b": -9.0, "c": -3.0, "d": 1.0, "e": -8.5, "f": -10.0}
    folds = (["a", "e", "c"], ["b", "d", "f"])

    runs = tp.cross_fit(LABELS, top1, folds)

    assert runs[0]["answerable_rejected"] == ["b"]


def test_verdict_defers_when_labels_are_not_independent_even_if_numbers_pass():
    verdict = tp.lock_verdict(
        labels_independent=False,
        cross_fit_answerable_rejected=[[], []],
        cross_fit_unrelated_accepted=[0, 0],
        floors=[-5.74, -6.32],
        lowest_answerable=-5.36,
    )

    assert verdict["decision"] == "defer"
    assert any("independent" in r for r in verdict["reasons"])


def test_verdict_defers_when_the_two_folds_floors_differ_by_more_than_the_margin():
    verdict = tp.lock_verdict(
        labels_independent=True,
        cross_fit_answerable_rejected=[[], []],
        cross_fit_unrelated_accepted=[0, 0],
        floors=[-5.74, -6.32],
        lowest_answerable=-5.36,
    )

    assert verdict["decision"] == "defer"
    assert any("floors differ" in r for r in verdict["reasons"])


def test_verdict_defers_when_an_answerable_question_is_rejected():
    verdict = tp.lock_verdict(
        labels_independent=True,
        cross_fit_answerable_rejected=[["p008"], []],
        cross_fit_unrelated_accepted=[0, 0],
        floors=[-6.0, -6.0],
        lowest_answerable=-5.0,
    )

    assert verdict["decision"] == "defer"
    assert any("p008" in r for r in verdict["reasons"])


def test_verdict_locks_only_when_every_criterion_holds():
    verdict = tp.lock_verdict(
        labels_independent=True,
        cross_fit_answerable_rejected=[[], []],
        cross_fit_unrelated_accepted=[0, 0],
        floors=[-6.0, -6.1],
        lowest_answerable=-5.0,
    )

    assert verdict["decision"] == "lock"
    assert verdict["reasons"] == []
