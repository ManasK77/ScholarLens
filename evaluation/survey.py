"""Cross-check generated gaps against survey-paper open problems."""

from dataclasses import dataclass

from evaluation.models import GapCandidate, SurveyRecord
from evaluation.similarity import text_similarity


@dataclass(frozen=True, slots=True)
class SurveyMatch:
    gap_id: str
    survey_id: str
    survey_title: str
    open_problem: str
    similarity: float


@dataclass(frozen=True, slots=True)
class SurveyCrosscheckResult:
    total_gaps: int
    matched_gap_count: int
    match_rate: float
    matches: tuple[SurveyMatch, ...]


def evaluate_survey_crosscheck(
    gaps: list[GapCandidate],
    surveys: list[SurveyRecord],
    similarity_threshold: float = 0.2,
) -> SurveyCrosscheckResult:
    if not 0 <= similarity_threshold <= 1:
        raise ValueError("similarity_threshold must be between 0 and 1")

    matches: list[SurveyMatch] = []
    for gap in gaps:
        best: SurveyMatch | None = None
        for survey in surveys:
            for problem in survey.open_problems:
                similarity = text_similarity(gap.description, problem)
                candidate = SurveyMatch(
                    gap.gap_id,
                    survey.survey_id,
                    survey.title,
                    problem,
                    round(similarity, 4),
                )
                if best is None or candidate.similarity > best.similarity:
                    best = candidate
        if best is not None and best.similarity >= similarity_threshold:
            matches.append(best)

    return SurveyCrosscheckResult(
        total_gaps=len(gaps),
        matched_gap_count=len(matches),
        match_rate=len(matches) / len(gaps) if gaps else 0.0,
        matches=tuple(matches),
    )
