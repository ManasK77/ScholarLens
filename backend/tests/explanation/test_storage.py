"""Stage 9's derived `gap_reports` table.

Like Stages 6, 7 and 8 this table is derived: `service.generate_top_reports`
recomputes it wholesale, so a rebuild replaces the previous run rather than
merging into it, and a Phase 6 cluster rebuild must clear it — cluster ids are
not stable across runs.
"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.clustering.schema import GapStatement, StatementCluster
from app.clustering.storage import replace_clusters
from app.explanation.schema import GapReportRecord
from app.explanation.storage import GapReport, list_reports, replace_reports
from app.ingestion import corpus

EVIDENCE = [
    {
        "id": "point-a",
        "paper_id": 101,
        "title": "Prompt injection at scale",
        "text": "Our evaluation covers English prompts only.",
        "year": 2023,
        "source_type": "limitation",
    }
]


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    corpus.Base.metadata.create_all(engine)
    with Session(engine) as active:
        yield active


def _cluster_ids(session, count: int) -> list[int]:
    return replace_clusters(
        session,
        [
            StatementCluster(
                label=f"theme-{index}",
                members=[
                    GapStatement.from_point(
                        {
                            "id": f"point-{index}-{member}",
                            "paper_id": 100 + member,
                            "text": "Evaluation covers English only.",
                            "year": 2023,
                            "source_type": "limitation",
                            "vector": [0.1, 0.2],
                        }
                    )
                    for member in range(2)
                ],
            )
            for index in range(count)
        ],
    )


def _record(cluster_id: int, **overrides) -> GapReportRecord:
    fields = {
        "cluster_id": cluster_id,
        "gap": "Multilingual prompt injection is underexplored.",
        "evidence": EVIDENCE,
        "confidence": 0.8,
        "suggested_directions": ["Multilingual benchmark"],
        "provider": "groq",
        "model": "groq-test",
    }
    fields.update(overrides)
    return GapReportRecord(**fields)


class TestReplaceReports:
    def test_it_returns_the_stored_cluster_ids_in_order(self, session):
        ids = _cluster_ids(session, 2)

        assert replace_reports(session, [_record(ids[0]), _record(ids[1])]) == ids

    def test_it_stores_every_field_the_api_serializes(self, session):
        [cluster_id] = _cluster_ids(session, 1)

        replace_reports(session, [_record(cluster_id)])

        [row] = list_reports(session)
        assert row.cluster_id == cluster_id
        assert row.gap == "Multilingual prompt injection is underexplored."
        assert row.evidence == EVIDENCE
        assert row.confidence == 0.8
        assert row.suggested_directions == ["Multilingual benchmark"]
        assert row.provider == "groq"
        assert row.model == "groq-test"

    def test_a_rebuild_replaces_rather_than_appends(self, session):
        ids = _cluster_ids(session, 2)
        replace_reports(session, [_record(ids[0]), _record(ids[1])])

        replace_reports(session, [_record(ids[1], gap="A different gap.")])

        [row] = list_reports(session)
        assert row.cluster_id == ids[1]
        assert row.gap == "A different gap."

    def test_an_empty_run_clears_the_previous_one(self, session):
        [cluster_id] = _cluster_ids(session, 1)
        replace_reports(session, [_record(cluster_id)])

        assert replace_reports(session, []) == []
        assert list_reports(session) == []

    def test_it_flushes_without_committing(self, session):
        """`routes_backend` owns the transaction, exactly as in Stages 6-8."""
        [cluster_id] = _cluster_ids(session, 1)

        replace_reports(session, [_record(cluster_id)])
        session.rollback()

        assert list_reports(session) == []


class TestListReports:
    def test_reports_come_back_in_the_order_they_were_generated(self, session):
        """Insertion order is rank order: the service writes best gap first."""
        ids = _cluster_ids(session, 3)
        ranked = [ids[2], ids[0], ids[1]]

        replace_reports(session, [_record(cluster_id) for cluster_id in ranked])

        assert [row.cluster_id for row in list_reports(session)] == ranked

    def test_the_payload_is_json_serializable(self, session):
        [cluster_id] = _cluster_ids(session, 1)
        replace_reports(session, [_record(cluster_id)])

        payload = [
            {
                "cluster_id": row.cluster_id,
                "gap": row.gap,
                "evidence": row.evidence,
                "confidence": row.confidence,
                "suggested_directions": row.suggested_directions,
                "provider": row.provider,
                "model": row.model,
            }
            for row in list_reports(session)
        ]

        assert json.loads(json.dumps(payload)) == payload


class TestClusterRebuildInvalidatesReports:
    def test_rebuilding_clusters_clears_stored_reports(self, session):
        """Cluster ids are recycled on SQLite; a stale report would reattach."""
        [cluster_id] = _cluster_ids(session, 1)
        replace_reports(session, [_record(cluster_id)])

        _cluster_ids(session, 1)

        assert list_reports(session) == []

    def test_the_table_hangs_off_clusters(self):
        foreign_keys = {
            key.column.table.name for key in GapReport.__table__.foreign_keys
        }

        assert foreign_keys == {"clusters"}
