"""Pairwise contradiction detection (CLUSTER-02).

Every detector built here passes `min_paper_pairs=1`. These tests are about
how a pair is scored, batched, truncated and ordered, and the hand-labelled
set contains exactly one disagreeing paper pair; the production default of
two is a corroboration policy, and it is tested where it belongs, in
test_nli_gates.py.
"""

import pytest

from app.clustering.nli import (
    CONTRADICTION,
    ENTAILMENT,
    NEUTRAL,
    ContradictionDetector,
    candidate_pairs,
    label_index,
)
from app.clustering.schema import GapStatement
from tests.clustering.labelled import CONTRADICTION_PAIR, synthetic_gap_points


@pytest.fixture
def statements() -> list[GapStatement]:
    return [GapStatement.from_point(point) for point in synthetic_gap_points()]


def _by_id(statements, point_id) -> GapStatement:
    return next(item for item in statements if item.point_id == point_id)


def _contradiction_pair(statements) -> list[GapStatement]:
    """The hand-labelled pair, which is the fixture's only gate-clearing one.

    `statements[:2]` no longer reaches the model: p-ml-1 and p-ml-2 describe
    the same theme in different words and share too little vocabulary to be
    "under the same setup" (D-06-11), so they are not compared at all.
    """
    return [_by_id(statements, pid) for pid in CONTRADICTION_PAIR]


class RecordingPredictor:
    """A stand-in for DeBERTa-MNLI that scores the hand-labelled pair only."""

    def __init__(self, contradicting: set[tuple[str, str]] | None = None) -> None:
        self.contradicting = contradicting or set()
        self.calls: list[list[tuple[str, str]]] = []

    def __call__(self, pairs: list[tuple[str, str]]) -> list[dict[str, float]]:
        self.calls.append(list(pairs))
        scores = []
        for premise, hypothesis in pairs:
            if (premise, hypothesis) in self.contradicting:
                scores.append({CONTRADICTION: 0.93, NEUTRAL: 0.05, ENTAILMENT: 0.02})
            else:
                scores.append({CONTRADICTION: 0.04, NEUTRAL: 0.80, ENTAILMENT: 0.16})
        return scores


class TestCandidatePairs:
    def test_pairs_every_distinct_cross_paper_combination(self, statements):
        subset = [_by_id(statements, pid) for pid in ("p-ml-1", "p-ml-2", "p-ml-3")]

        pairs = candidate_pairs(subset, max_pairs=100)

        assert len(pairs) == 3
        assert all(left.paper_id != right.paper_id for left, right in pairs)

    def test_same_paper_statements_are_never_paired(self, statements):
        # D-06-04: two sentences from one paper disagreeing is an extraction
        # artefact, not gap evidence.
        subset = [_by_id(statements, pid) for pid in ("p-hl-3", "p-hl-4")]
        assert subset[0].paper_id == subset[1].paper_id

        assert candidate_pairs(subset, max_pairs=100) == []

    def test_pair_count_is_capped(self, statements):
        pairs = candidate_pairs(statements, max_pairs=5)

        assert len(pairs) == 5

    def test_pairs_are_deterministic(self, statements):
        first = candidate_pairs(statements, max_pairs=8)
        second = candidate_pairs(statements, max_pairs=8)

        assert [(a.point_id, b.point_id) for a, b in first] == [
            (a.point_id, b.point_id) for a, b in second
        ]

    def test_a_non_positive_cap_is_rejected(self, statements):
        with pytest.raises(ValueError, match="max_pairs"):
            candidate_pairs(statements, max_pairs=0)

    def test_the_cap_is_applied_before_the_pairs_are_materialized(self):
        """The O(n^2) list must never exist — only the capped prefix.

        A 2000-member cluster is ~2M candidate pairs. Building them all and
        then slicing to 50 is the exact resource failure README 13.6 warns
        about, so the cap has to bound the *enumeration*, not just the result.
        """

        class CountingStatement:
            __slots__ = ("_paper_id", "point_id", "reads")

            def __init__(self, index, reads):
                self.point_id = f"p-{index:05d}"
                self._paper_id = index
                self.reads = reads

            @property
            def paper_id(self):
                self.reads[0] += 1
                return self._paper_id

        reads = [0]
        cluster = [CountingStatement(index, reads) for index in range(2000)]

        pairs = candidate_pairs(cluster, max_pairs=50)

        assert len(pairs) == 50
        # Two reads per examined pair, plus a small constant. Anything near
        # 2000*1999 means the whole product was enumerated.
        assert reads[0] < 1000


class TestContradictionDetector:
    def test_flags_the_hand_labelled_cross_paper_disagreement(self, statements):
        left, right = (_by_id(statements, pid) for pid in CONTRADICTION_PAIR)
        # Both orders, because a real disagreement contradicts either way round
        # and D-06-12 now requires that.
        predictor = RecordingPredictor(
            {(left.text, right.text), (right.text, left.text)}
        )
        detector = ContradictionDetector(predictor, threshold=0.5, min_paper_pairs=1)

        found = detector.detect([left, right])

        assert len(found) == 1
        assert {found[0].left_point_id, found[0].right_point_id} == set(
            CONTRADICTION_PAIR
        )
        assert found[0].score == pytest.approx(0.93)

    def test_both_directions_are_scored(self, statements):
        # NLI is asymmetric (D-06-05), so both orders are put to the model.
        left, right = (_by_id(statements, pid) for pid in CONTRADICTION_PAIR)
        predictor = RecordingPredictor({(right.text, left.text)})
        detector = ContradictionDetector(predictor, threshold=0.5, min_paper_pairs=1)

        detector.detect([left, right])

        scored = {pair for call in predictor.calls for pair in call}
        assert (left.text, right.text) in scored
        assert (right.text, left.text) in scored

    def test_one_direction_alone_no_longer_flags_a_pair(self, statements):
        # D-06-12: a contradiction in one order only is as likely an ordering
        # artefact as a disagreement, so it no longer counts. The stronger
        # direction is still what gets stored once both agree -- see
        # test_nli_gates.py::TestBothDirections.
        left, right = (_by_id(statements, pid) for pid in CONTRADICTION_PAIR)
        predictor = RecordingPredictor({(right.text, left.text)})
        detector = ContradictionDetector(predictor, threshold=0.5, min_paper_pairs=1)

        assert detector.detect([left, right]) == []

    def test_agreeing_statements_are_not_flagged(self, statements):
        detector = ContradictionDetector(RecordingPredictor(), threshold=0.5)

        assert detector.detect(statements[:4]) == []

    def test_a_score_below_the_threshold_is_not_flagged(self, statements):
        left, right = (_by_id(statements, pid) for pid in CONTRADICTION_PAIR)
        predictor = RecordingPredictor(
            {(left.text, right.text), (right.text, left.text)}
        )
        detector = ContradictionDetector(predictor, threshold=0.99, min_paper_pairs=1)

        assert detector.detect([left, right]) == []

    def test_findings_are_ordered_by_score_descending(self, statements):
        first, third, fourth = (
            _by_id(statements, pid) for pid in ("p-ml-1", "p-ml-3", "p-ml-4")
        )
        weaker = frozenset((first.text, third.text))
        stronger = frozenset((third.text, fourth.text))

        class Graded(RecordingPredictor):
            """Symmetric scores, so both directions clear the threshold."""

            def __call__(self, batch):
                out = []
                for premise, hypothesis in batch:
                    pair = frozenset((premise, hypothesis))
                    if pair == weaker:
                        out.append({CONTRADICTION: 0.7, NEUTRAL: 0.2, ENTAILMENT: 0.1})
                    elif pair == stronger:
                        out.append(
                            {CONTRADICTION: 0.9, NEUTRAL: 0.05, ENTAILMENT: 0.05}
                        )
                    else:
                        out.append(
                            {CONTRADICTION: 0.01, NEUTRAL: 0.9, ENTAILMENT: 0.09}
                        )
                return out

        detector = ContradictionDetector(Graded(), threshold=0.5, min_paper_pairs=1)
        found = detector.detect([first, third, fourth])

        assert [round(pair.score, 2) for pair in found] == [0.9, 0.7]

    def test_a_single_statement_needs_no_model_call(self, statements):
        predictor = RecordingPredictor()
        detector = ContradictionDetector(predictor, threshold=0.5, min_paper_pairs=1)

        assert detector.detect(statements[:1]) == []
        assert predictor.calls == []

    def test_text_is_truncated_before_it_reaches_the_model(self, statements):
        predictor = RecordingPredictor()
        detector = ContradictionDetector(predictor, threshold=0.5, max_chars=10)

        detector.detect(statements[:2])

        assert all(
            len(premise) <= 10 and len(hypothesis) <= 10
            for call in predictor.calls
            for premise, hypothesis in call
        )

    def test_predictions_are_batched(self, statements):
        predictor = RecordingPredictor()
        detector = ContradictionDetector(
            predictor, threshold=0.5, batch_size=2, min_paper_pairs=1
        )

        detector.detect(statements[:4])

        assert predictor.calls
        assert all(len(call) <= 2 for call in predictor.calls)

    def test_a_wrong_length_prediction_is_rejected(self, statements):
        detector = ContradictionDetector(
            lambda pairs: [], threshold=0.5, min_paper_pairs=1
        )

        with pytest.raises(ValueError, match="predictions"):
            detector.detect(_contradiction_pair(statements))

    def test_a_non_finite_probability_is_rejected_clearly(self, statements):
        # NaN survives min()/max() untouched and would otherwise surface as an
        # opaque "score must be in [0, 1], got nan" from ContradictionPair.
        detector = ContradictionDetector(
            lambda pairs: [{CONTRADICTION: float("nan")} for _ in pairs],
            threshold=0.5,
            min_paper_pairs=1,
        )

        with pytest.raises(ValueError, match="finite"):
            detector.detect(_contradiction_pair(statements))

    def test_a_missing_contradiction_key_is_rejected(self, statements):
        detector = ContradictionDetector(
            lambda pairs: [{NEUTRAL: 1.0} for _ in pairs],
            threshold=0.5,
            min_paper_pairs=1,
        )

        with pytest.raises(ValueError, match="contradiction"):
            detector.detect(_contradiction_pair(statements))

    def test_a_threshold_outside_zero_one_is_rejected(self):
        with pytest.raises(ValueError, match="threshold"):
            ContradictionDetector(RecordingPredictor(), threshold=1.5)

    def test_max_pairs_bounds_the_forward_passes(self, statements):
        predictor = RecordingPredictor()
        detector = ContradictionDetector(
            predictor, threshold=0.5, max_pairs_per_cluster=3, min_paper_pairs=1
        )

        detector.detect(statements)

        scored = sum(len(call) for call in predictor.calls)
        assert scored == 6  # 3 pairs, both directions


class TestLabelIndex:
    def test_maps_a_checkpoint_label_set_onto_the_canonical_names(self):
        # D-06-03: DeBERTa MNLI releases disagree on label order, so the mapping
        # is read from the checkpoint rather than hardcoded.
        mapping = label_index({"CONTRADICTION": 0, "NEUTRAL": 1, "ENTAILMENT": 2})

        assert mapping == {CONTRADICTION: 0, NEUTRAL: 1, ENTAILMENT: 2}

    def test_accepts_lowercase_and_shorthand_label_names(self):
        mapping = label_index({"entailment": 0, "neutral": 1, "contradiction": 2})

        assert mapping[CONTRADICTION] == 2
        assert mapping[ENTAILMENT] == 0

    def test_rejects_a_checkpoint_that_is_not_three_way_nli(self):
        with pytest.raises(ValueError, match="NLI"):
            label_index({"POSITIVE": 0, "NEGATIVE": 1})
