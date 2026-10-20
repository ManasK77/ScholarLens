from evaluation.models import GapCandidate, PaperRecord
from evaluation.time_shift import evaluate_time_shift


def test_time_shift_only_matches_papers_after_each_gap_cutoff():
    gaps = [
        GapCandidate(
            gap_id="gap-1",
            description="Multilingual prompt injection defenses remain underexplored",
            cutoff_year=2022,
            relevant_future_paper_ids=("paper-new",),
        )
    ]
    papers = [
        PaperRecord(
            "paper-old", "Multilingual prompt injection defenses", 2021, "A benchmark."
        ),
        PaperRecord(
            "paper-new",
            "Multilingual prompt injection benchmark",
            2024,
            "We evaluate defenses across twelve languages.",
        ),
        PaperRecord(
            "paper-other", "Efficient vision models", 2024, "A compression method."
        ),
    ]

    result = evaluate_time_shift(gaps, papers, similarity_threshold=0.2)

    assert result.addressed_count == 1
    assert result.addressed_rate == 1.0
    assert result.matches[0].paper_id == "paper-new"
    assert result.precision == 1.0
    assert result.recall == 1.0


# --- D-13-04: an unlabelled gap is not "no relevant later papers" ------------------

PAPERS = [
    PaperRecord(
        "p1", "Multilingual prompt injection benchmark", 2024, "Twelve languages."
    ),
    PaperRecord("p2", "Bangla jailbreak defense evaluation", 2024, "Bangla prompts."),
]


def _gap(gap_id, description, labels):
    return GapCandidate(gap_id, description, 2022, labels)


def test_predictions_for_an_unlabelled_gap_are_not_false_positives():
    gaps = [
        _gap("a", "Multilingual prompt injection benchmark", ("p1",)),
        _gap("b", "Bangla jailbreak defense evaluation", None),
    ]
    result = evaluate_time_shift(gaps, PAPERS, similarity_threshold=0.2)
    assert result.addressed_count == 2  # discovery still counts every gap
    assert result.labelled_gaps == 1
    assert result.precision == 1.0 and result.recall == 1.0


def test_a_gap_labelled_with_no_relevant_papers_does_count_its_matches():
    gaps = [
        _gap("a", "Multilingual prompt injection benchmark", ("p1",)),
        _gap("b", "Bangla jailbreak defense evaluation", ()),
    ]
    result = evaluate_time_shift(gaps, PAPERS, similarity_threshold=0.2)
    assert result.labelled_gaps == 2
    assert result.precision == 0.5


def test_precision_without_any_prediction_is_undefined_not_zero():
    gaps = [_gap("a", "Quantum error correction for qubits", ("p1",))]
    result = evaluate_time_shift(gaps, PAPERS, similarity_threshold=0.2)
    assert result.precision is None and result.recall == 0.0


def test_a_missing_or_null_label_loads_as_unlabelled():
    row = {"gap_id": "g", "description": "d", "cutoff_year": 2024}
    assert GapCandidate.from_dict(row).relevant_future_paper_ids is None
    assert (
        GapCandidate.from_dict(
            row | {"relevant_future_paper_ids": None}
        ).relevant_future_paper_ids
        is None
    )
    assert (
        GapCandidate.from_dict(
            row | {"relevant_future_paper_ids": []}
        ).relevant_future_paper_ids
        == ()
    )
