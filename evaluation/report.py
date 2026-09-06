"""Load evaluation inputs, run all strategies, and persist one report."""

import csv
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from evaluation.expert import evaluate_expert_scores
from evaluation.models import ExpertScore, GapCandidate, PaperRecord, SurveyRecord
from evaluation.survey import evaluate_survey_crosscheck
from evaluation.time_shift import evaluate_time_shift


def _json_rows(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise TypeError(f"{path} must contain a JSON array")
    return payload


def _expert_rows(path: Path) -> list[ExpertScore]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [ExpertScore.from_dict(row) for row in csv.DictReader(handle)]


def run_evaluation(
    gaps_path: Path,
    future_papers_path: Path,
    surveys_path: Path,
    expert_scores_path: Path,
    output_path: Path,
    similarity_threshold: float = 0.2,
) -> dict:
    gaps = [GapCandidate.from_dict(row) for row in _json_rows(gaps_path)]
    papers = [PaperRecord.from_dict(row) for row in _json_rows(future_papers_path)]
    surveys = [SurveyRecord.from_dict(row) for row in _json_rows(surveys_path)]
    scores = _expert_rows(expert_scores_path)

    known_gap_ids = {gap.gap_id for gap in gaps}
    unknown_scores = sorted({score.gap_id for score in scores} - known_gap_ids)
    if unknown_scores:
        raise ValueError(
            f"expert scores reference unknown gaps: {', '.join(unknown_scores)}"
        )

    time_shift = evaluate_time_shift(gaps, papers, similarity_threshold)
    survey = evaluate_survey_crosscheck(gaps, surveys, similarity_threshold)
    expert = evaluate_expert_scores(scores)
    components = [
        time_shift.addressed_rate,
        survey.match_rate,
        expert.mean_validity / 5 if expert.total_ratings else 0.0,
    ]
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "configuration": {"similarity_threshold": similarity_threshold},
        "summary": {
            "gaps_evaluated": len(gaps),
            "validity_index": round(sum(components) / len(components), 4),
            "interpretation": "Composite of later-paper confirmation, survey agreement, and normalized expert validity.",
        },
        "time_shift": asdict(time_shift),
        "survey_crosscheck": asdict(survey),
        "expert_scoring": asdict(expert),
    }
    # Normalize tuples from immutable result records into JSON-native lists so
    # the returned value is identical to the persisted artifact.
    report = json.loads(json.dumps(report))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
