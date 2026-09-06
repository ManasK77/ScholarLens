from evaluation.models import GapCandidate, SurveyRecord
from evaluation.survey import evaluate_survey_crosscheck


def test_survey_crosscheck_reports_matching_open_problems():
    gaps = [
        GapCandidate("gap-1", "Low-resource multilingual evaluation is missing", 2023),
        GapCandidate("gap-2", "Energy usage is rarely measured", 2023),
    ]
    surveys = [
        SurveyRecord(
            survey_id="survey-1",
            title="A Survey of Multilingual NLP",
            year=2024,
            open_problems=(
                "Evaluation for low-resource multilingual systems remains limited",
            ),
        )
    ]

    result = evaluate_survey_crosscheck(gaps, surveys, similarity_threshold=0.2)

    assert result.matched_gap_count == 1
    assert result.match_rate == 0.5
    assert result.matches[0].gap_id == "gap-1"
