"""Validate whether gaps identified at date X were addressed after X."""

from dataclasses import dataclass

from evaluation.models import GapCandidate, PaperRecord
from evaluation.similarity import text_similarity


@dataclass(frozen=True, slots=True)
class TimeShiftMatch:
    gap_id: str
    paper_id: str
    paper_title: str
    paper_year: int
    similarity: float


@dataclass(frozen=True, slots=True)
class TimeShiftResult:
    total_gaps: int
    addressed_count: int
    addressed_rate: float
    matches: tuple[TimeShiftMatch, ...]
    precision: float | None
    recall: float | None
    # Gaps with a `relevant_future_paper_ids` label; precision and recall
    # are computed over these only (D-13-04).
    labelled_gaps: int = 0


def evaluate_time_shift(
    gaps: list[GapCandidate],
    future_papers: list[PaperRecord],
    similarity_threshold: float = 0.2,
) -> TimeShiftResult:
    if not 0 <= similarity_threshold <= 1:
        raise ValueError("similarity_threshold must be between 0 and 1")

    matches: list[TimeShiftMatch] = []
    predicted: set[tuple[str, str]] = set()
    truth: set[tuple[str, str]] = set()
    addressed: set[str] = set()
    labelled = {g.gap_id for g in gaps if g.relevant_future_paper_ids is not None}
    for gap in gaps:
        truth.update(
            (gap.gap_id, paper_id) for paper_id in gap.relevant_future_paper_ids or ()
        )
        candidates: list[TimeShiftMatch] = []
        for paper in future_papers:
            if paper.year <= gap.cutoff_year:
                continue
            similarity = text_similarity(gap.description, paper.searchable_text)
            if similarity >= similarity_threshold:
                candidates.append(
                    TimeShiftMatch(
                        gap.gap_id,
                        paper.paper_id,
                        paper.title,
                        paper.year,
                        round(similarity, 4),
                    )
                )
                predicted.add((gap.gap_id, paper.paper_id))
        if candidates:
            addressed.add(gap.gap_id)
            matches.extend(
                sorted(candidates, key=lambda item: item.similarity, reverse=True)
            )

    # An unlabelled gap's matches are neither right nor wrong: they are
    # excluded, not counted as false positives (D-13-04).
    scored = {pair for pair in predicted if pair[0] in labelled}
    true_positives = len(scored & truth)
    # No prediction among labelled gaps: precision is undefined, not 0.
    precision = true_positives / len(scored) if labelled and scored else None
    recall = true_positives / len(truth) if truth else None
    return TimeShiftResult(
        total_gaps=len(gaps),
        addressed_count=len(addressed),
        addressed_rate=len(addressed) / len(gaps) if gaps else 0.0,
        matches=tuple(matches),
        precision=precision,
        recall=recall,
        labelled_gaps=len(labelled),
    )
