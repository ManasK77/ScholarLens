"""Read-only export of stored gap reports for the Phase 13 harness."""

import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.clustering.schema import GapStatement, StatementCluster
from app.clustering.storage import list_clusters, replace_clusters
from app.explanation.export import EXPORT_SCHEMA, ExportError, export_reports
from app.explanation.schema import GapReportRecord
from app.explanation.storage import GapReport, replace_reports
from app.ingestion import corpus
from app.ranking.signals import GapSignals
from app.ranking.storage import replace_rankings


def _member(point, paper):
    return GapStatement(
        point, paper, f"Statement {point}.", 2024, "limitation", (0.1, 0.2)
    )


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    corpus.Base.metadata.create_all(engine)
    with Session(engine) as db:
        for paper_id, year in ((1, 2024), (2, 2025), (3, 2026)):
            db.add(
                corpus.Paper(
                    id=paper_id, title=f"P{paper_id}", year=year, source="arxiv"
                )
            )
        replace_clusters(
            db,
            [
                StatementCluster(
                    label="multilingual gaps",
                    members=[_member("a", 1), _member("b", 2)],
                ),
                StatementCluster(
                    label="single paper theme",
                    members=[_member("c", 3), _member("d", 3)],
                ),
            ],
        )
        first, second = (c.id for c in list_clusters(db))
        replace_rankings(
            db,
            [
                (first, 2.5, GapSignals(2, 1.0, 0.0, 0.0, 0.0)),
                (second, -3.0, GapSignals(1, 0.0, 1.0, 0.0, 0.0)),
            ],
        )
        replace_reports(
            db,
            [
                GapReportRecord(
                    cluster_id=first,
                    gap="Multilingual defences are untested.",
                    evidence=[
                        {
                            "id": "e1",
                            "paper_id": 2,
                            "title": "P2",
                            "text": "x",
                            "year": 2025,
                            "source_type": "limitation",
                        },
                        {
                            "id": "e2",
                            "paper_id": 1,
                            "title": "P1",
                            "text": "y",
                            "year": 2024,
                            "source_type": "paragraph",
                        },
                    ],
                    confidence=0.95,
                    suggested_directions=["Benchmark more languages"],
                    provider="groq",
                    model="m",
                    justification={
                        "problem_evidence": {
                            "supported": True,
                            "statement": "s",
                            "evidence_ids": ["e1"],
                            "basis": "llm_cited",
                        }
                    },
                ),
                GapReportRecord(
                    cluster_id=second,
                    gap="A one-paper lead.",
                    evidence=[
                        {
                            "id": "e3",
                            "paper_id": 3,
                            "title": "P3",
                            "text": "z",
                            "year": 2026,
                            "source_type": "limitation",
                        }
                    ],
                    confidence=0.4,
                    suggested_directions=["Check it"],
                    provider="groq",
                    model="m",
                ),
            ],
        )
        db.commit()
        yield db


def test_every_stored_report_is_exported_with_its_provenance(session):
    document = export_reports(session)

    assert document["export_schema"] == EXPORT_SCHEMA
    assert document["corpus"] == {"papers": 3, "min_year": 2024, "max_year": 2026}
    first, second = document["reports"]
    assert first["report_id"] < second["report_id"]
    assert first["cluster_label"] == "multilingual gaps"
    assert first["cluster_paper_ids"] == [1, 2]
    assert first["supporting_paper_ids"] == [1, 2]
    assert [e["evidence_id"] for e in first["evidence"]] == ["e1", "e2"]
    assert first["ranking"] == {
        "rank": 1,
        "score": 2.5,
        "paper_count": 2,
        "evidence_scope": "corroborated",
    }
    assert second["ranking"]["evidence_scope"] == "single_paper"
    assert len(first["content_sha256"]) == 64
    assert first["created_at"]


def test_generated_content_is_kept_apart_from_provenance(session):
    report = export_reports(session)["reports"][0]
    generated = report["generated"]
    assert generated["gap"] == "Multilingual defences are untested."
    assert generated["model_confidence"] == 0.95
    assert generated["claims"]["problem_evidence"]["basis"] == "llm_cited"
    # The model's own numbers live only under `generated`; nothing in the
    # export is an expert rating or a validation outcome.
    assert "model_confidence" not in {k for k in report if k != "generated"}
    assert not {"validity", "novelty", "expert", "validated"} & set(
        json.dumps(report).split('"')
    )


def test_two_exports_of_the_same_store_are_identical(session):
    assert json.dumps(export_reports(session), sort_keys=True) == json.dumps(
        export_reports(session), sort_keys=True
    )


def test_the_export_writes_nothing(session):
    before = session.scalar(select(func.count()).select_from(GapReport))
    export_reports(session)
    assert not session.new and not session.dirty and not session.deleted
    assert session.scalar(select(func.count()).select_from(GapReport)) == before


@pytest.mark.parametrize(
    "evidence",
    [
        [],
        [{"paper_id": 1}],
        [{"id": "e1"}],
        [{"id": "", "paper_id": 1}],
        ["not an object"],
    ],
)
def test_missing_or_malformed_evidence_fails_clearly(session, evidence):
    row = session.scalars(select(GapReport)).first()
    row.evidence = evidence
    session.flush()
    with pytest.raises(ExportError, match=f"report {row.id}"):
        export_reports(session)
