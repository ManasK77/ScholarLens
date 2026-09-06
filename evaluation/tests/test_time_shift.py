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
