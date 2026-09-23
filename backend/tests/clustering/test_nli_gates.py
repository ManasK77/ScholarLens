"""Stage 6's corroboration rules for a contradiction (D-06-10 … D-06-12).

Every statement in `TestClusterOneFalsePositives` is real: they are the exact
five statements behind the four pairs that flagged cluster 1 of the 20-paper
LLM-security corpus as "Conflicting evidence". None of them is a disagreement —
one is an ethics/availability disclaimer paired against a recommendation
bullet, a related-work description, a future-work proposal and one genuine
limitation. The NLI model scored all four at P(contradiction) > 0.999.

These tests pin the four rules that make that impossible now: both sides must
be claim-like, the two must be topically related, both NLI directions must
agree, and one paper-pair is not enough on its own.
"""

import pytest

from app.clustering.nli import CONTRADICTION, ENTAILMENT, NEUTRAL, ContradictionDetector
from app.clustering.schema import GapStatement

# --- cluster 1's real statements --------------------------------------------

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
LIMITATION = (
    "The variation in vulnerabilities across different prompt designs "
    "underscores the limitations of current defenses and the need for "
    "adaptive safeguards capable of addressing a range of attack vectors"
)

# --- a real disagreement, from the same corpus ------------------------------
#
# Paper 5 reports that current prompt injection defenses do not hold; paper 9
# (Sentra-Guard) reports that its prompt injection defense holds in every
# tested configuration. Same subject, opposite outcome.
DEFENSES_FAIL = (
    "Current prompt injection defenses fail to generalize across models and "
    "leave deployed systems vulnerable to adaptive attacks."
)
DEFENSES_HOLD = (
    "Our prompt injection defense generalizes across models and leaves "
    "deployed systems protected against adaptive attacks in every tested "
    "configuration."
)
DEFENSES_PARTIAL = (
    "Prompt injection defenses generalize across models and protect deployed "
    "systems against adaptive attacks when the attack budget is bounded."
)


def statement(point_id: str, paper_id: int, text: str) -> GapStatement:
    return GapStatement(
        point_id=point_id,
        paper_id=paper_id,
        text=text,
        year=2024,
        source_type="limitation",
        vector=(1.0, 0.0),
    )


class AlwaysContradicts:
    """The failure mode under test: an NLI head that says "contradiction" always.

    DeBERTa-MNLI scored cluster 1's four unrelated pairs above 0.999, so a
    detector that only asks the model is a detector with no gate at all.
    """

    def __init__(self, score: float = 0.9995) -> None:
        self.score = score
        self.calls: list[list[tuple[str, str]]] = []

    def __call__(self, pairs: list[tuple[str, str]]) -> list[dict[str, float]]:
        self.calls.append(list(pairs))
        remainder = (1.0 - self.score) / 2
        return [
            {CONTRADICTION: self.score, NEUTRAL: remainder, ENTAILMENT: remainder}
            for _ in pairs
        ]


class OneDirectionOnly:
    """Contradicts only when `premise` comes first — an NLI order artefact."""

    def __init__(self, premise: str, hypothesis: str) -> None:
        self.ordered = (premise, hypothesis)

    def __call__(self, pairs: list[tuple[str, str]]) -> list[dict[str, float]]:
        return [
            {CONTRADICTION: 0.99, NEUTRAL: 0.005, ENTAILMENT: 0.005}
            if pair == self.ordered
            else {CONTRADICTION: 0.02, NEUTRAL: 0.9, ENTAILMENT: 0.08}
            for pair in pairs
        ]


def detector(predict, **overrides) -> ContradictionDetector:
    settings = {"threshold": 0.7, "min_paper_pairs": 1}
    settings.update(overrides)
    return ContradictionDetector(predict, **settings)


class TestClusterOneFalsePositives:
    @pytest.mark.parametrize(
        ("text", "paper_id"),
        [
            (RECOMMENDATION, 5),
            (RELATED_WORK, 10),
            (PROPOSAL, 5),
            (LIMITATION, 5),
        ],
        ids=["recommendation", "related-work", "future-work", "limitation"],
    )
    def test_no_pair_against_the_disclaimer_is_flagged(
        self, text: str, paper_id: int
    ) -> None:
        # The exact four pairs stored for cluster 1, against an NLI model that
        # scores every one of them 0.9995 — as the real one did.
        pairs = detector(AlwaysContradicts()).detect(
            [statement("a", paper_id, text), statement("b", 2, DISCLAIMER)]
        )

        assert pairs == []

    def test_all_four_stored_pairs_disappear_at_once(self) -> None:
        cluster = [
            statement("m1", 5, RECOMMENDATION),
            statement("m2", 10, RELATED_WORK),
            statement("m3", 5, PROPOSAL),
            statement("m4", 5, LIMITATION),
            statement("m5", 2, DISCLAIMER),
        ]

        assert detector(AlwaysContradicts()).detect(cluster) == []

    def test_the_disclaimer_never_reaches_the_model(self) -> None:
        # Not merely filtered afterwards: a non-claim is not worth a forward
        # pass, and the gate has to hold even if the model is replaced.
        predict = AlwaysContradicts()
        detector(predict).detect(
            [statement("a", 5, LIMITATION), statement("b", 2, DISCLAIMER)]
        )

        assert all(
            DISCLAIMER not in premise and DISCLAIMER not in hypothesis
            for call in predict.calls
            for premise, hypothesis in call
        )


class TestGenuineContradiction:
    def test_an_opposing_claim_about_the_same_subject_is_flagged(self) -> None:
        found = detector(AlwaysContradicts()).detect(
            [statement("a", 5, DEFENSES_FAIL), statement("b", 9, DEFENSES_HOLD)]
        )

        assert len(found) == 1
        assert {found[0].left_paper_id, found[0].right_paper_id} == {5, 9}
        assert found[0].score == pytest.approx(0.9995)

    def test_a_genuine_pair_survives_every_gate_but_corroboration(self) -> None:
        # The same pair under the production default: real, but uncorroborated.
        found = ContradictionDetector(
            AlwaysContradicts(), threshold=0.7, min_paper_pairs=2
        ).detect([statement("a", 5, DEFENSES_FAIL), statement("b", 9, DEFENSES_HOLD)])

        assert found == []


class TestTopicGate:
    def test_unrelated_statements_are_never_compared(self) -> None:
        predict = AlwaysContradicts()
        found = detector(predict).detect(
            [
                statement("a", 5, DEFENSES_FAIL),
                statement(
                    "b",
                    9,
                    "The annotation budget limited every dataset in this "
                    "survey to a single human rater per example.",
                ),
            ]
        )

        assert found == []
        assert predict.calls == []

    def test_thresholds_come_from_the_detector(self) -> None:
        pair = [statement("a", 5, DEFENSES_FAIL), statement("b", 9, DEFENSES_HOLD)]

        assert detector(AlwaysContradicts(), min_shared_terms=99).detect(pair) == []
        assert detector(AlwaysContradicts(), min_topic_jaccard=0.99).detect(pair) == []


class TestBothDirections:
    def test_a_one_directional_contradiction_is_not_enough(self) -> None:
        # NLI is asymmetric, so a single direction over threshold is as likely
        # an ordering artefact as a disagreement.
        found = detector(OneDirectionOnly(DEFENSES_FAIL, DEFENSES_HOLD)).detect(
            [statement("a", 5, DEFENSES_FAIL), statement("b", 9, DEFENSES_HOLD)]
        )

        assert found == []

    def test_one_direction_suffices_when_the_rule_is_switched_off(self) -> None:
        found = detector(
            OneDirectionOnly(DEFENSES_FAIL, DEFENSES_HOLD),
            require_both_directions=False,
        ).detect([statement("a", 5, DEFENSES_FAIL), statement("b", 9, DEFENSES_HOLD)])

        assert len(found) == 1

    def test_the_stored_score_is_still_the_stronger_direction(self) -> None:
        # D-06-05 is unchanged for *scoring*; both directions are a gate on
        # whether the pair counts, not a new way to score it.
        class Lopsided:
            def __call__(self, pairs):
                return [
                    {CONTRADICTION: 0.99, NEUTRAL: 0.005, ENTAILMENT: 0.005}
                    if pair == (DEFENSES_FAIL, DEFENSES_HOLD)
                    else {CONTRADICTION: 0.80, NEUTRAL: 0.1, ENTAILMENT: 0.1}
                    for pair in pairs
                ]

        found = detector(Lopsided()).detect(
            [statement("a", 5, DEFENSES_FAIL), statement("b", 9, DEFENSES_HOLD)]
        )

        assert found[0].score == pytest.approx(0.99)


class TestCorroboration:
    def test_one_paper_pair_is_not_enough_by_default(self) -> None:
        found = ContradictionDetector(AlwaysContradicts(), threshold=0.7).detect(
            [statement("a", 5, DEFENSES_FAIL), statement("b", 9, DEFENSES_HOLD)]
        )

        assert found == []

    def test_two_distinct_paper_pairs_are_enough(self) -> None:
        found = ContradictionDetector(AlwaysContradicts(), threshold=0.7).detect(
            [
                statement("a", 5, DEFENSES_FAIL),
                statement("b", 9, DEFENSES_HOLD),
                statement("c", 11, DEFENSES_PARTIAL),
            ]
        )

        assert len({frozenset((p.left_paper_id, p.right_paper_id)) for p in found}) >= 2

    def test_many_pairs_between_the_same_two_papers_are_still_one_pair(self) -> None:
        # Cluster 1's four "pairs" were three statements from paper 5 and one
        # from paper 10, all against the *same* statement from paper 2. Counting
        # statements rather than papers is what made one disclaimer look like
        # four independent disagreements.
        found = ContradictionDetector(AlwaysContradicts(), threshold=0.7).detect(
            [
                statement("a1", 5, DEFENSES_FAIL),
                statement(
                    "a2",
                    5,
                    "Prompt injection defenses fail to generalize across "
                    "deployed models under adaptive attacks of any budget.",
                ),
                statement("b", 9, DEFENSES_HOLD),
            ]
        )

        assert found == []

    def test_findings_are_ordered_and_deterministic(self) -> None:
        cluster = [
            statement("a", 5, DEFENSES_FAIL),
            statement("b", 9, DEFENSES_HOLD),
            statement("c", 11, DEFENSES_PARTIAL),
        ]
        first = ContradictionDetector(AlwaysContradicts(), threshold=0.7).detect(
            cluster
        )
        second = ContradictionDetector(AlwaysContradicts(), threshold=0.7).detect(
            list(reversed(cluster))
        )

        assert [(p.left_point_id, p.right_point_id) for p in first] == [
            (p.left_point_id, p.right_point_id) for p in second
        ]
