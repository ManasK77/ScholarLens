"""Aggregate the 1–5 expert novelty and validity rubric."""

from dataclasses import dataclass
from statistics import fmean, pstdev

from evaluation.models import ExpertScore


@dataclass(frozen=True, slots=True)
class GapExpertSummary:
    gap_id: str
    rater_count: int
    mean_novelty: float
    mean_validity: float
    mean_evidence_quality: float
    mean_actionability: float
    validity_stddev: float


@dataclass(frozen=True, slots=True)
class ExpertEvaluationResult:
    total_ratings: int
    gaps_rated: int
    mean_novelty: float
    mean_validity: float
    mean_evidence_quality: float
    mean_actionability: float
    high_validity_fraction: float
    per_gap: tuple[GapExpertSummary, ...]


def _mean(scores: list[ExpertScore], field: str) -> float:
    return fmean(getattr(score, field) for score in scores) if scores else 0.0


def evaluate_expert_scores(
    scores: list[ExpertScore], valid_threshold: int = 4
) -> ExpertEvaluationResult:
    if not 1 <= valid_threshold <= 5:
        raise ValueError("valid_threshold must be between 1 and 5")
    grouped: dict[str, list[ExpertScore]] = {}
    for score in scores:
        grouped.setdefault(score.gap_id, []).append(score)

    per_gap = []
    for gap_id, gap_scores in sorted(grouped.items()):
        validity = [score.validity for score in gap_scores]
        per_gap.append(
            GapExpertSummary(
                gap_id=gap_id,
                rater_count=len(gap_scores),
                mean_novelty=_mean(gap_scores, "novelty"),
                mean_validity=_mean(gap_scores, "validity"),
                mean_evidence_quality=_mean(gap_scores, "evidence_quality"),
                mean_actionability=_mean(gap_scores, "actionability"),
                validity_stddev=pstdev(validity) if len(validity) > 1 else 0.0,
            )
        )

    high_validity = sum(score.validity >= valid_threshold for score in scores)
    return ExpertEvaluationResult(
        total_ratings=len(scores),
        gaps_rated=len(grouped),
        mean_novelty=_mean(scores, "novelty"),
        mean_validity=_mean(scores, "validity"),
        mean_evidence_quality=_mean(scores, "evidence_quality"),
        mean_actionability=_mean(scores, "actionability"),
        high_validity_fraction=high_validity / len(scores) if scores else 0.0,
        per_gap=tuple(per_gap),
    )
