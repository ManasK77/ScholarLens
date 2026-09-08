"""Persistence for per-cluster trends (TREND-02).

The stored row is what `GET /trends` serializes, so the round trip through the
JSON column — not just the in-memory dict — is what these tests pin down.
"""

import json
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.clustering.schema import GapStatement, StatementCluster
from app.clustering.storage import replace_clusters
from app.ingestion import corpus
from app.trends.analysis import TrendSeries
from app.trends.storage import ClusterTrend, list_trends, replace_trends


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    corpus.Base.metadata.create_all(engine)
    with Session(engine) as active:
        yield active


def _statement(point_id: str, paper_id: int, year: int | None):
    return GapStatement.from_point(
        {
            "id": point_id,
            "paper_id": paper_id,
            "text": f"Evaluation is limited to English ({point_id}).",
            "year": year,
            "source_type": "limitation",
            "vector": [0.1, 0.2, 0.3],
        }
    )


@pytest.fixture()
def cluster_ids(session) -> list[int]:
    """Two real Phase 6 clusters, so `cluster_id` points at something."""
    return replace_clusters(
        session,
        [
            StatementCluster(
                label="multilingual",
                members=[_statement("p1", 101, 2021), _statement("p2", 102, 2023)],
            ),
            StatementCluster(label="compute", members=[_statement("p3", 103, 2024)]),
        ],
    )


def _series(counts: dict[str, int], slope: float = 0.0, undated: int = 0):
    return TrendSeries(yearly_counts=counts, slope=slope, undated_mentions=undated)


class TestReplaceTrends:
    def test_returns_the_cluster_ids_it_stored(self, session, cluster_ids):
        stored = replace_trends(
            session,
            [
                (cluster_ids[0], _series({"2021": 1, "2022": 0, "2023": 1}, 0.0)),
                (cluster_ids[1], _series({"2024": 1}, 0.0)),
            ],
        )

        assert stored == cluster_ids

    def test_stores_one_row_per_cluster(self, session, cluster_ids):
        replace_trends(
            session,
            [
                (cluster_ids[0], _series({"2021": 2}, 0.0)),
                (cluster_ids[1], _series({}, 0.0)),
            ],
        )

        assert len(list_trends(session)) == 2

    def test_the_yearly_counts_survive_the_json_round_trip_with_string_keys(
        self, session, cluster_ids
    ):
        # `frontend/src/types.ts` declares `Record<string, number>` and
        # `TrendChart.tsx` reads `yearly_counts[String(year)]`.
        counts = {"2020": 1, "2021": 0, "2022": 4}
        replace_trends(session, [(cluster_ids[0], _series(counts, 1.5))])
        session.expire_all()

        [row] = list_trends(session)

        assert row.yearly_counts == counts
        assert all(isinstance(key, str) for key in row.yearly_counts)
        assert all(isinstance(value, int) for value in row.yearly_counts.values())

    def test_the_stored_slope_is_a_float(self, session, cluster_ids):
        replace_trends(session, [(cluster_ids[0], _series({"2021": 1}, -0.75))])
        session.expire_all()

        [row] = list_trends(session)

        assert row.slope == -0.75
        assert isinstance(row.slope, float)

    def test_an_empty_series_is_stored_rather_than_skipped(self, session, cluster_ids):
        # D-07-07: Phase 8 must not have to tell "no trend" from "not computed".
        replace_trends(session, [(cluster_ids[0], _series({}, 0.0, undated=3))])
        session.expire_all()

        [row] = list_trends(session)

        assert row.yearly_counts == {}
        assert row.slope == 0.0

    def test_a_rebuild_replaces_rather_than_accumulates(self, session, cluster_ids):
        replace_trends(session, [(cluster_ids[0], _series({"2021": 1}, 0.0))])
        replace_trends(session, [(cluster_ids[1], _series({"2024": 9}, 2.0))])

        rows = list_trends(session)

        assert [row.cluster_id for row in rows] == [cluster_ids[1]]
        assert rows[0].yearly_counts == {"2024": 9}

    def test_replacing_with_nothing_clears_the_table(self, session, cluster_ids):
        replace_trends(session, [(cluster_ids[0], _series({"2021": 1}, 0.0))])

        assert replace_trends(session, []) == []
        assert list_trends(session) == []

    def test_it_flushes_but_never_commits(self, session, cluster_ids):
        # `routes_backend` owns the transaction and commits after the call.
        replace_trends(session, [(cluster_ids[0], _series({"2021": 1}, 0.0))])

        assert session.in_transaction()
        assert session.scalars(select(ClusterTrend)).all()

        session.rollback()

        assert session.scalars(select(ClusterTrend)).all() == []


class TestListTrends:
    def test_rows_come_back_in_cluster_id_order(self, session, cluster_ids):
        replace_trends(
            session,
            [
                (cluster_ids[1], _series({"2024": 1}, 0.0)),
                (cluster_ids[0], _series({"2021": 1}, 0.0)),
            ],
        )

        assert [row.cluster_id for row in list_trends(session)] == sorted(cluster_ids)

    def test_an_empty_table_lists_nothing(self, session):
        assert list_trends(session) == []

    def test_the_rows_serialize_exactly_as_the_frozen_route_projects_them(
        self, session, cluster_ids
    ):
        # `routes_backend.trends()` builds this dict verbatim.
        replace_trends(
            session, [(cluster_ids[0], _series({"2021": 1, "2022": 3}, 2.0))]
        )
        session.expire_all()

        payload = [
            {
                "cluster_id": row.cluster_id,
                "yearly_counts": row.yearly_counts,
                "slope": row.slope,
            }
            for row in list_trends(session)
        ]

        assert json.loads(json.dumps(payload)) == [
            {
                "cluster_id": cluster_ids[0],
                "yearly_counts": {"2021": 1, "2022": 3},
                "slope": 2.0,
            }
        ]


class TestStaleTrendsCannotSurviveARecluster:
    """A trend only means anything for the cluster set that produced it.

    On PostgreSQL the foreign key's `ON DELETE CASCADE` guarantees this. SQLite
    — the documented local-dev store — enforces no foreign keys and reuses
    rowids after a full delete, so a bare `POST /clusters` would otherwise leave
    the old theme's series attached to a brand-new, unrelated cluster and
    `GET /trends` would report a slope for the wrong theme.
    """

    def test_reclustering_drops_the_trends_of_the_old_cluster_set(
        self, session, cluster_ids
    ):
        replace_trends(
            session, [(cluster_ids[0], _series({"2020": 1, "2024": 1}, 0.1))]
        )

        # Phase 6 runs again on its own and discovers a completely different
        # theme, which on SQLite is handed the id the old cluster just freed.
        new_ids = replace_clusters(
            session,
            [
                StatementCluster(
                    label="compute cost", members=[_statement("p9", 909, 2025)]
                )
            ],
        )

        assert list_trends(session) == [], (
            f"a trend computed for cluster {cluster_ids[0]} survived a recluster "
            f"and is now attached to {new_ids[0]}"
        )


def test_the_storage_layer_is_cheap_to_import():
    """`app/main.py` imports this module purely to register the table.

    It must not drag Pandas into application startup for the sake of one type
    annotation — the same property `app/clustering/schema.py` protects for the
    piece Stage 6's storage depends on. A subprocess, because the rest of this
    test session has already imported the world.
    """
    probe = (
        "import sys; import app.trends.storage; "
        "sys.exit(1 if 'pandas' in sys.modules else 0)"
    )

    assert (
        subprocess.run([sys.executable, "-c", probe], check=False).returncode == 0
    ), "importing app.trends.storage pulled in pandas"


class TestSchema:
    def test_a_cluster_can_hold_only_one_trend(self):
        assert ClusterTrend.__table__.c.cluster_id.unique is True

    def test_the_trend_table_cascades_from_clusters(self):
        # Phase 6 rebuilds wipe `clusters`; without the cascade that DELETE
        # fails on PostgreSQL the moment a trend row exists.
        [key] = list(ClusterTrend.__table__.foreign_keys)

        assert key.column.table.name == "clusters"
        assert key.ondelete == "CASCADE"
