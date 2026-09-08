"""Phase 8 persistence (GAPRANK-02) against a real SQLAlchemy session.

`cluster_rankings` is derived, like Stage 6's and Stage 7's tables: a rebuild
replaces the previous run wholesale. The consumer contract is
`app/api/routes_backend.py`'s `GET /gaps`.
"""

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.clustering.storage import Cluster, replace_clusters
from app.ingestion.corpus import Base
from app.ranking.signals import GapSignals
from app.ranking.storage import ClusterRanking, list_rankings, replace_rankings


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


def _cluster(session, label: str = "multilingual") -> int:
    row = Cluster(
        label=label, member_count=3, contradiction_flag=False, contradiction_score=0.0
    )
    session.add(row)
    session.flush()
    return row.id


def _signals(**overrides) -> GapSignals:
    values = {
        "mention_frequency": 7,
        "trend_slope": 2.5,
        "solution_density": 0.25,
        "citation_impact": 12.0,
        "contradiction_flag": 1.0,
    }
    values.update(overrides)
    return GapSignals(**values)


def test_a_ranking_stores_the_score_and_every_signal(session) -> None:
    """The breakdown is the audit trail; a bare score would not be evidence."""
    cluster_id = _cluster(session)

    replace_rankings(session, [(cluster_id, 8.4, _signals())])

    row = session.scalar(select(ClusterRanking))
    assert row.cluster_id == cluster_id
    assert row.score == 8.4
    assert row.mention_frequency == 7
    assert row.trend_slope == 2.5
    assert row.solution_density == 0.25
    assert row.citation_impact == 12.0
    assert row.contradiction_flag == 1.0


def test_replacing_rankings_wipes_the_previous_run(session) -> None:
    cluster_id = _cluster(session)
    replace_rankings(session, [(cluster_id, 8.4, _signals())])

    replace_rankings(session, [(cluster_id, 1.0, _signals(mention_frequency=1))])

    rows = list(session.scalars(select(ClusterRanking)))
    assert len(rows) == 1
    assert rows[0].score == 1.0


def test_replace_rankings_returns_the_cluster_ids_in_order(session) -> None:
    """`POST /gaps` returns them to the caller under the key `clusters`."""
    ids = [_cluster(session, f"theme {index}") for index in range(3)]

    assert (
        replace_rankings(session, [(cluster_id, 1.0, _signals()) for cluster_id in ids])
        == ids
    )


def test_replace_rankings_flushes_but_never_commits(session) -> None:
    """`routes_backend` owns the transaction."""
    cluster_id = _cluster(session)

    replace_rankings(session, [(cluster_id, 8.4, _signals())])
    session.rollback()

    assert session.scalar(select(ClusterRanking)) is None


def test_list_rankings_returns_the_highest_score_first(session) -> None:
    """`App.tsx` numbers the rows `index + 1` and calls them "Ranked research gaps"."""
    low, high, middle = (_cluster(session, f"theme {i}") for i in range(3))
    replace_rankings(
        session,
        [
            (low, 1.0, _signals()),
            (high, 9.0, _signals()),
            (middle, 5.0, _signals()),
        ],
    )

    assert [row.cluster_id for row in list_rankings(session, 10)] == [high, middle, low]


def test_ties_are_broken_by_cluster_id(session) -> None:
    """A printed rank must not depend on insertion order."""
    second, first = _cluster(session, "b"), _cluster(session, "a")
    replace_rankings(session, [(second, 4.0, _signals()), (first, 4.0, _signals())])

    assert [row.cluster_id for row in list_rankings(session, 10)] == sorted(
        [first, second]
    )


def test_list_rankings_honours_the_limit(session) -> None:
    ids = [_cluster(session, f"theme {index}") for index in range(5)]
    replace_rankings(
        session,
        [(cluster_id, float(cluster_id), _signals()) for cluster_id in ids],
    )

    assert len(list_rankings(session, 2)) == 2


def test_list_rankings_on_an_empty_table_returns_empty(session) -> None:
    assert list_rankings(session, 10) == []


def test_a_negative_score_still_ranks(session) -> None:
    """A heavily-solved theme can score below zero; it is ranked last, not dropped."""
    negative, positive = _cluster(session, "a"), _cluster(session, "b")
    replace_rankings(
        session, [(negative, -12.5, _signals()), (positive, 0.5, _signals())]
    )

    assert [row.cluster_id for row in list_rankings(session, 10)] == [
        positive,
        negative,
    ]


def test_a_rebuild_of_clusters_clears_the_rankings(session) -> None:
    """Cluster ids are not stable across Phase 6 rebuilds.

    `replace_clusters._dependent_tables()` discovers this table from its foreign
    key, so a stale ranking cannot end up attached to an unrelated theme on
    SQLite, where rowids are recycled and foreign keys are not enforced.
    """
    cluster_id = _cluster(session)
    replace_rankings(session, [(cluster_id, 8.4, _signals())])

    replace_clusters(session, [])

    assert session.scalar(select(ClusterRanking)) is None


def test_the_table_has_one_row_per_cluster(session) -> None:
    """`GET /gaps` must never show a cluster twice with two different scores."""
    indexes = inspect(session.get_bind()).get_indexes("cluster_rankings")

    assert any(
        index["unique"] and index["column_names"] == ["cluster_id"] for index in indexes
    )
