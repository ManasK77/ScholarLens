"""Load evaluation inputs, run all strategies, and persist one report.

Each method needs independent evidence -- later papers, survey open problems,
expert ratings. When that evidence is absent the method is reported as
`unavailable` with what it needs, never scored as zero, and the validity
index averages only the methods that were computed (`null` if none were).
"""

import csv
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from evaluation.expert import evaluate_expert_scores
from evaluation.models import (
    SCORE_FIELDS,
    ExpertScore,
    GapCandidate,
    PaperRecord,
    SurveyRecord,
)
from evaluation.survey import evaluate_survey_crosscheck
from evaluation.time_shift import evaluate_time_shift

REQUIRES = {
    "time_shift": "papers published after the gaps' cutoff year (--future-papers), "
    "ideally with relevant_future_paper_ids labels",
    "survey_crosscheck": "open problems transcribed from survey papers (--surveys)",
    "expert_scoring": "1-5 ratings from independent experts (--expert-scores)",
}


def _json_rows(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise TypeError(f"{path} must contain a JSON array")
    return payload


def _expert_rows(path: Path) -> list[ExpertScore]:
    """Rated rows only. A template row with every score blank is not yet rated
    and is skipped; a partly filled row is an error, not a guess."""
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [
        ExpertScore.from_dict(row)
        for row in rows
        if any(str(row.get(field) or "").strip() for field in SCORE_FIELDS)
    ]


def _unavailable(method: str, reason: str) -> dict:
    return {"status": "unavailable", "reason": reason, "requires": REQUIRES[method]}


def run_evaluation(
    gaps_path: Path,
    future_papers_path: Path | None,
    surveys_path: Path | None,
    expert_scores_path: Path | None,
    output_path: Path,
    similarity_threshold: float = 0.2,
) -> dict:
    gaps = [GapCandidate.from_dict(row) for row in _json_rows(gaps_path)]
    papers = (
        [PaperRecord.from_dict(row) for row in _json_rows(future_papers_path)]
        if future_papers_path
        else []
    )
    surveys = (
        [SurveyRecord.from_dict(row) for row in _json_rows(surveys_path)]
        if surveys_path
        else []
    )
    scores = _expert_rows(expert_scores_path) if expert_scores_path else []

    known_gap_ids = {gap.gap_id for gap in gaps}
    unknown_scores = sorted({score.gap_id for score in scores} - known_gap_ids)
    if unknown_scores:
        raise ValueError(
            f"expert scores reference unknown gaps: {', '.join(unknown_scores)}"
        )

    availability: dict[str, dict] = {}
    components: dict[str, float] = {}

    time_shift = None
    later = [p for p in papers if any(p.year > gap.cutoff_year for gap in gaps)]
    if not later:
        availability["time_shift"] = _unavailable(
            "time_shift",
            "no paper published after the cutoff year"
            if papers
            else "no later papers supplied",
        )
    else:
        time_shift = evaluate_time_shift(gaps, papers, similarity_threshold)
        availability["time_shift"] = {"status": "computed"}
        components["time_shift"] = time_shift.addressed_rate

    survey = None
    if not surveys:
        availability["survey_crosscheck"] = _unavailable(
            "survey_crosscheck", "no survey open problems supplied"
        )
    else:
        survey = evaluate_survey_crosscheck(gaps, surveys, similarity_threshold)
        availability["survey_crosscheck"] = {"status": "computed"}
        components["survey_crosscheck"] = survey.match_rate

    expert = None
    if not scores:
        availability["expert_scoring"] = _unavailable(
            "expert_scoring", "no expert ratings supplied"
        )
    else:
        expert = evaluate_expert_scores(scores)
        availability["expert_scoring"] = {"status": "computed"}
        # The 1-5 rubric mapped onto 0-1: "unsupported or false" scores 0.
        components["expert_scoring"] = (expert.mean_validity - 1) / 4

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "configuration": {"similarity_threshold": similarity_threshold},
        "summary": {
            "gaps_evaluated": len(gaps),
            "validity_index": round(sum(components.values()) / len(components), 4)
            if components
            else None,
            "components_used": sorted(components),
            "interpretation": "Composite of later-paper confirmation, survey "
            "agreement, and normalized expert validity, over the methods that "
            "had independent evidence.",
        },
        "availability": availability,
        "time_shift": asdict(time_shift) if time_shift else None,
        "survey_crosscheck": asdict(survey) if survey else None,
        "expert_scoring": asdict(expert) if expert else None,
    }
    # Normalize tuples from immutable result records into JSON-native lists so
    # the returned value is identical to the persisted artifact.
    report = json.loads(json.dumps(report))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
