"""Evaluating ScholarLens's own stored gap reports."""

import copy
import csv
import json

import pytest

from evaluation import scholarlens
from evaluation.report import run_evaluation

# Shaped like `backend/app/explanation/export.py` output for the live corpus.
EXPORT = {
    "export_schema": "scholarlens-gap-export/1",
    "export_digest": "d" * 64,
    "source": {"table": "gap_reports", "derived_state": {"s9": "f" * 64}},
    "corpus": {"papers": 20, "min_year": 2023, "max_year": 2026},
    "reports": [
        {
            "report_id": 13,
            "cluster_id": 13,
            "created_at": "2026-10-12T19:15:48",
            "content_sha256": "b" * 64,
            "cluster_label": "adaptive adversarial defense",
            "cluster_paper_ids": [7],
            "ranking": {
                "rank": 4,
                "score": -24.0,
                "paper_count": 1,
                "evidence_scope": "single_paper",
            },
            "evidence": [
                {
                    "evidence_id": "e3",
                    "paper_id": 7,
                    "year": 2025,
                    "title": "P7",
                    "source_type": "limitation",
                }
            ],
            "supporting_paper_ids": [7],
            "generated": {
                "gap": "Bangla defences are untested.",
                "suggested_directions": ["x"],
                "model_confidence": 0.9,
                "provider": "groq",
                "model": "m",
                "claims": {},
            },
        },
        {
            "report_id": 10,
            "cluster_id": 11,
            "created_at": "2026-10-12T19:15:48",
            "content_sha256": "a" * 64,
            "cluster_label": "attacks adversarial attack",
            "cluster_paper_ids": [1, 2],
            "ranking": {
                "rank": 1,
                "score": -0.37,
                "paper_count": 9,
                "evidence_scope": "corroborated",
            },
            "evidence": [
                {
                    "evidence_id": "e1",
                    "paper_id": 2,
                    "year": 2025,
                    "title": "P2",
                    "source_type": "limitation",
                },
                {
                    "evidence_id": "e2",
                    "paper_id": 1,
                    "year": 2024,
                    "title": "P1",
                    "source_type": "paragraph",
                },
            ],
            "supporting_paper_ids": [1, 2],
            "generated": {
                "gap": "Adaptive attacks bypass multilingual prompt injection defences.",
                "suggested_directions": ["Benchmark adaptive attacks"],
                "model_confidence": 0.85,
                "provider": "groq",
                "model": "m",
                "claims": {
                    "problem_evidence": {
                        "basis": "llm_cited",
                        "supported": True,
                        "evidence_ids": ["e1"],
                    }
                },
            },
        },
    ],
}


def _write(tmp_path, document=EXPORT):
    path = tmp_path / "export.json"
    path.write_text(json.dumps(document))
    return path


def test_reports_become_harness_gaps_with_stable_ids_and_the_corpus_cutoff():
    gaps = scholarlens.gaps_input(scholarlens.validate_export(copy.deepcopy(EXPORT)))
    assert [g["gap_id"] for g in gaps] == [
        "scholarlens:cluster-11:report-10",
        "scholarlens:cluster-13:report-13",
    ]
    assert (
        gaps[0]["description"]
        == "Adaptive attacks bypass multilingual prompt injection defences."
    )
    assert {g["cutoff_year"] for g in gaps} == {2026}
    # No invented labels: unlabelled is null; [] would mean "labelled, none relevant".
    assert all(g["relevant_future_paper_ids"] is None for g in gaps)


def test_provenance_traces_every_gap_to_its_report_cluster_and_evidence(tmp_path):
    out = tmp_path / "out"
    scholarlens.run(_write(tmp_path), out)
    record = json.loads((out / "provenance.json").read_text())
    gap = record["gaps"]["scholarlens:cluster-11:report-10"]
    assert (gap["report_id"], gap["cluster_id"]) == (10, 11)
    assert gap["created_at"] == "2026-10-12T19:15:48"
    assert gap["content_sha256"] == "a" * 64
    assert gap["supporting_paper_ids"] == [1, 2]
    assert gap["evidence_ids"] == ["e1", "e2"]
    assert record["export_schema"] == "scholarlens-gap-export/1"
    assert record["export_digest"] == "d" * 64
    assert record["adapter_version"] == "scholarlens-adapter/1"


def test_without_independent_evidence_nothing_is_scored(tmp_path):
    report = scholarlens.run(_write(tmp_path), tmp_path / "out")
    assert report["summary"]["validity_index"] is None
    assert report["summary"]["components_used"] == []
    assert {m: s["status"] for m, s in report["availability"].items()} == {
        "time_shift": "unavailable",
        "survey_crosscheck": "unavailable",
        "expert_scoring": "unavailable",
    }
    assert report["time_shift"] is None and report["expert_scoring"] is None


def test_model_confidence_and_claims_are_never_treated_as_expert_ratings(tmp_path):
    out = tmp_path / "out"
    report = scholarlens.run(_write(tmp_path), out)
    assert (
        report["expert_scoring"] is None
    )  # 0.85 / 0.9 model confidence is not a rating
    with (out / "expert-scores-template.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert [r["gap_id"] for r in rows] == [
        "scholarlens:cluster-11:report-10",
        "scholarlens:cluster-13:report-13",
    ]
    assert all(not r["rater_id"] and not r["validity"] for r in rows)
    # The untouched template is accepted as input and still scores nothing.
    again = run_evaluation(
        out / "gaps.json",
        None,
        None,
        out / "expert-scores-template.csv",
        out / "r.json",
    )
    assert again["availability"]["expert_scoring"]["status"] == "unavailable"


def test_real_expert_ratings_use_the_corrected_validity_mapping(tmp_path):
    out = tmp_path / "out"
    scholarlens.run(_write(tmp_path), out)
    ratings = out / "ratings.csv"
    ratings.write_text(
        "gap_id,rater_id,novelty,validity,evidence_quality,actionability,notes\n"
        "scholarlens:cluster-11:report-10,r1,4,5,4,4,\n"
        "scholarlens:cluster-13:report-13,r1,3,1,2,3,\n"
    )
    report = scholarlens.run(_write(tmp_path), out, expert_scores=ratings)
    # mean validity 3 -> (3 - 1) / 4 = 0.5; the only computed component.
    assert report["summary"]["validity_index"] == 0.5
    assert report["summary"]["components_used"] == ["expert_scoring"]


def test_partly_filled_ratings_fail_instead_of_being_guessed(tmp_path):
    out = tmp_path / "out"
    scholarlens.run(_write(tmp_path), out)
    ratings = out / "ratings.csv"
    ratings.write_text(
        "gap_id,rater_id,novelty,validity,evidence_quality,actionability,notes\n"
        "scholarlens:cluster-11:report-10,r1,4,,4,4,\n"
    )
    with pytest.raises(ValueError, match="validity is missing"):
        scholarlens.run(_write(tmp_path), out, expert_scores=ratings)


def test_exports_and_outputs_are_deterministic(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    scholarlens.run(_write(tmp_path), first)
    scholarlens.run(_write(tmp_path), second)
    for name in ("gaps.json", "provenance.json", "expert-scores-template.csv"):
        assert (first / name).read_bytes() == (second / name).read_bytes()


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda d: d.update(export_schema="something-else"),
        lambda d: d.update(reports=[]),
        lambda d: d["corpus"].update(max_year=None),
        lambda d: d["reports"][0].update(evidence=[]),
        lambda d: d["reports"][0]["evidence"][0].pop("evidence_id"),
        lambda d: d["reports"][1]["generated"].update(gap="  "),
        lambda d: d["reports"][0].update(report_id="13"),
    ],
)
def test_malformed_exports_fail_clearly_and_are_never_replaced_by_examples(
    tmp_path, corrupt
):
    document = copy.deepcopy(EXPORT)
    corrupt(document)
    with pytest.raises(ValueError):
        scholarlens.run(_write(tmp_path, document), tmp_path / "out")
    assert not (tmp_path / "out" / "gaps.json").exists()
