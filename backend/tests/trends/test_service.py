"""Stage 7 orchestration: persisted Phase 6 clusters in, persisted trends out.

This phase touches no model, no vector store and no network — its entire input
is `clusters` + `cluster_members`, which is why these tests build real Phase 6
rows through `replace_clusters` rather than mocking the read side.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.clustering.schema import GapStatement, StatementCluster
from app.clustering.storage import replace_clusters
from app.ingestion import corpus
from app.trends.analysis import DEFAULT_MAX_YEAR, DEFAULT_MIN_YEAR
from app.trends.service import rebuild_trends
from app.trends.storage import list_trends


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    corpus.Base.metadata.create_all(engine)
    with Session(engine) as active:
        yield active


@pytest.fixture()
def settings() -> SimpleNamespace:
    """Only the two fields Stage 7 reads — `tests/core/test_config.py` owns
    proving `Settings` actually carries them."""
    return SimpleNamespace(
        trend_min_year=DEFAULT_MIN_YEAR, trend_max_year=DEFAULT_MAX_YEAR
    )


def _statement(point_id: str, paper_id: int, year: int | None):
    return GapStatement.from_point(
        {
            "id": point_id,
            "paper_id": paper_id,
            "text": f"Coverage of low-resource languages is limited ({point_id}).",
            "year": year,
            "source_type": "limitation",
            "vector": [0.4, 0.5, 0.6],
        }
    )


def _seed(session, clusters: list[list[tuple[str, int, int | None]]]) -> list[int]:
    return replace_clusters(
        session,
        [
            StatementCluster(
                label=f"theme-{position}",
                members=[_statement(*member) for member in members],
            )
            for position, members in enumerate(clusters)
        ],
    )


class TestRebuildTrends:
    def test_returns_a_cluster_id_per_cluster(self, session, settings):
        cluster_ids = _seed(
            session,
            [
                [("a1", 101, 2021), ("a2", 102, 2022), ("a3", 103, 2022)],
                [("b1", 201, 2024)],
            ],
        )

        assert rebuild_trends(session, settings) == cluster_ids

    def test_aggregates_each_cluster_independently(self, session, settings):
        cluster_ids = _seed(
            session,
            [
                [("a1", 101, 2021), ("a2", 102, 2022), ("a3", 103, 2022)],
                [("b1", 201, 2023), ("b2", 202, 2023)],
            ],
        )
        rebuild_trends(session, settings)

        rows = {row.cluster_id: row for row in list_trends(session)}

        assert rows[cluster_ids[0]].yearly_counts == {"2021": 1, "2022": 2}
        assert rows[cluster_ids[0]].slope == 1.0
        assert rows[cluster_ids[1]].yearly_counts == {"2023": 2}
        assert rows[cluster_ids[1]].slope == 0.0

    def test_a_paper_contributing_several_statements_counts_once(
        self, session, settings
    ):
        # D-07-01 end to end: paper 101 put three sentences in this cluster.
        [cluster_id] = _seed(
            session,
            [[("a1", 101, 2023), ("a2", 101, 2023), ("a3", 102, 2023)]],
        )
        rebuild_trends(session, settings)

        [row] = list_trends(session)

        assert row.cluster_id == cluster_id
        assert row.yearly_counts == {"2023": 2}

    def test_a_cluster_with_only_undated_members_still_gets_a_row(
        self, session, settings
    ):
        _seed(session, [[("a1", 101, None), ("a2", 102, None)]])
        rebuild_trends(session, settings)

        [row] = list_trends(session)

        assert row.yearly_counts == {}
        assert row.slope == 0.0

    def test_sparse_years_are_zero_filled_from_the_stored_members(
        self, session, settings
    ):
        _seed(session, [[("a1", 101, 2019), ("a2", 102, 2022), ("a3", 103, 2022)]])
        rebuild_trends(session, settings)

        [row] = list_trends(session)

        assert row.yearly_counts == {"2019": 1, "2020": 0, "2021": 0, "2022": 2}

    def test_no_clusters_stores_nothing_and_returns_nothing(self, session, settings):
        assert rebuild_trends(session, settings) == []
        assert list_trends(session) == []

    def test_a_rebuild_leaves_no_trace_of_the_previous_run(self, session, settings):
        _seed(session, [[("a1", 101, 2021), ("a2", 102, 2022)]])
        rebuild_trends(session, settings)

        # Phase 6 reruns and produces different clusters with new ids.
        new_ids = _seed(session, [[("b1", 201, 2024), ("b2", 202, 2025)]])
        assert rebuild_trends(session, settings) == new_ids

        rows = list_trends(session)

        assert [row.cluster_id for row in rows] == new_ids
        assert rows[0].yearly_counts == {"2024": 1, "2025": 1}

    def test_running_twice_over_the_same_clusters_is_idempotent(
        self, session, settings
    ):
        _seed(session, [[("a1", 101, 2021), ("a2", 102, 2023)]])

        first = rebuild_trends(session, settings)
        first_rows = [
            (row.cluster_id, row.yearly_counts, row.slope)
            for row in list_trends(session)
        ]
        second = rebuild_trends(session, settings)
        second_rows = [
            (row.cluster_id, row.yearly_counts, row.slope)
            for row in list_trends(session)
        ]

        assert first == second
        assert first_rows == second_rows

    def test_the_configured_year_window_is_honoured(self, session, settings):
        _seed(session, [[("a1", 101, 1974), ("a2", 102, 2024)]])
        narrowed = SimpleNamespace(trend_min_year=2000, trend_max_year=DEFAULT_MAX_YEAR)

        rebuild_trends(session, narrowed)

        [row] = list_trends(session)

        assert row.yearly_counts == {"2024": 1}

    def test_it_flushes_but_never_commits(self, session, settings):
        _seed(session, [[("a1", 101, 2021), ("a2", 102, 2022)]])
        rebuild_trends(session, settings)

        assert session.in_transaction()

        session.rollback()

        assert list_trends(session) == []

    def test_settings_are_resolved_lazily_when_not_supplied(
        self, session, settings, monkeypatch
    ):
        # `routes_backend` calls `rebuild_trends(session)` with the session only.
        monkeypatch.setattr("app.trends.service.get_settings", lambda: settings)
        cluster_ids = _seed(session, [[("a1", 101, 2021), ("a2", 102, 2022)]])

        assert rebuild_trends(session) == cluster_ids
