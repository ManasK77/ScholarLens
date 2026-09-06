import json

from evaluation.report import run_evaluation


def test_harness_writes_a_concrete_summary_report(tmp_path):
    gaps = tmp_path / "gaps.json"
    papers = tmp_path / "future-papers.json"
    surveys = tmp_path / "surveys.json"
    scores = tmp_path / "expert-scores.csv"
    output = tmp_path / "report.json"

    gaps.write_text(
        json.dumps(
            [
                {
                    "gap_id": "gap-1",
                    "description": "Multilingual prompt injection defenses remain underexplored",
                    "cutoff_year": 2022,
                    "relevant_future_paper_ids": ["paper-1"],
                }
            ]
        )
    )
    papers.write_text(
        json.dumps(
            [
                {
                    "paper_id": "paper-1",
                    "title": "A multilingual prompt injection benchmark",
                    "year": 2024,
                    "abstract": "We evaluate defenses across many languages.",
                }
            ]
        )
    )
    surveys.write_text(
        json.dumps(
            [
                {
                    "survey_id": "survey-1",
                    "title": "Security survey",
                    "year": 2024,
                    "open_problems": [
                        "Multilingual prompt injection evaluation remains underexplored"
                    ],
                }
            ]
        )
    )
    scores.write_text(
        "gap_id,rater_id,novelty,validity,evidence_quality,actionability,notes\n"
        "gap-1,expert-1,5,5,4,4,Strong evidence\n"
    )

    report = run_evaluation(
        gaps, papers, surveys, scores, output, similarity_threshold=0.2
    )

    persisted = json.loads(output.read_text())
    assert report["summary"]["gaps_evaluated"] == 1
    assert report["summary"]["validity_index"] == 1.0
    assert persisted == report
    assert report["time_shift"]["precision"] == 1.0
    assert report["survey_crosscheck"]["match_rate"] == 1.0
