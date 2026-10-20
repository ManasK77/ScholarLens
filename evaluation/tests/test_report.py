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


def test_lowest_expert_validity_contributes_nothing_to_the_index(tmp_path):
    """README: mean expert validity is "normalized to 0-1" -- so the bottom of
    the 1-5 rubric ("Unsupported or false") is 0, not 0.2."""
    gaps = tmp_path / "gaps.json"
    papers = tmp_path / "future-papers.json"
    surveys = tmp_path / "surveys.json"
    scores = tmp_path / "expert-scores.csv"
    gaps.write_text(
        json.dumps(
            [{"gap_id": "gap-1", "description": "Quantum widgets", "cutoff_year": 2022}]
        )
    )
    papers.write_text(
        json.dumps([{"paper_id": "p", "title": "Sparse image labels", "year": 2025}])
    )
    surveys.write_text(
        json.dumps(
            [
                {
                    "survey_id": "s",
                    "title": "t",
                    "year": 2024,
                    "open_problems": ["Unrelated topic"],
                }
            ]
        )
    )
    scores.write_text(
        "gap_id,rater_id,novelty,validity,evidence_quality,actionability,notes\n"
        "gap-1,expert-1,1,1,1,1,Unsupported\n"
    )

    report = run_evaluation(gaps, papers, surveys, scores, tmp_path / "r.json")

    assert report["time_shift"]["addressed_rate"] == 0.0
    assert report["survey_crosscheck"]["match_rate"] == 0.0
    assert report["summary"]["validity_index"] == 0.0


def test_the_documented_example_scores_are_unchanged(tmp_path):
    from pathlib import Path

    examples = Path(__file__).resolve().parent.parent / "examples"
    report = run_evaluation(
        examples / "gaps.json",
        examples / "future-papers.json",
        examples / "surveys.json",
        examples / "expert-scores.csv",
        tmp_path / "r.json",
        similarity_threshold=0.15,
    )
    committed = json.loads((examples / "evaluation-report.json").read_text())
    assert (
        report["summary"]["validity_index"]
        == committed["summary"]["validity_index"]
        == 0.7083
    )
    assert report["summary"]["components_used"] == [
        "expert_scoring",
        "survey_crosscheck",
        "time_shift",
    ]
    # `labelled_gaps` (D-13-04) is additive; the committed report predates it.
    assert report["time_shift"].pop("labelled_gaps") == 3
    for method in ("time_shift", "survey_crosscheck", "expert_scoring"):
        assert report[method] == committed[method]
