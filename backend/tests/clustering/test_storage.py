"""Persistence for clusters, members and contradiction flags (CLUSTER-03)."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.clustering.schema import ContradictionPair, GapStatement, StatementCluster
from app.clustering.storage import (
    Cluster,
    ClusterContradiction,
    ClusterMember,
    contradictions_for_cluster,
    list_clusters,
    members_for_cluster,
    replace_clusters,
)
from app.ingestion import corpus
from tests.clustering.labelled import CONTRADICTION_PAIR, synthetic_gap_points


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    corpus.Base.metadata.create_all(engine)
    with Session(engine) as active:
        yield active


@pytest.fixture()
def statements() -> list[GapStatement]:
    return [GapStatement.from_point(point) for point in synthetic_gap_points()]


def _cluster(statements, label="multilingual gap", contradictions=None):
    return StatementCluster(
        label=label, members=statements, contradictions=contradictions or []
    )


class TestReplaceClusters:
    def test_returns_the_persisted_primary_keys(self, session, statements):
        ids = replace_clusters(
            session, [_cluster(statements[:4]), _cluster(statements[4:8], "faithful")]
        )

        assert len(ids) == 2
        assert all(isinstance(value, int) for value in ids)
        assert ids == [row.id for row in list_clusters(session)]

    def test_stores_the_member_statements(self, session, statements):
        [cluster_id] = replace_clusters(session, [_cluster(statements[:4])])

        members = members_for_cluster(session, cluster_id)

        assert [member.point_id for member in members] == [
            statement.point_id for statement in statements[:4]
        ]
        assert members[0].paper_id == 101
        assert members[0].year == 2023
        assert members[0].text.startswith("Our evaluation is limited to English")

    def test_member_count_matches_the_stored_members(self, session, statements):
        replace_clusters(session, [_cluster(statements[:4])])

        [row] = list_clusters(session)
        assert row.member_count == 4
        assert row.member_count == len(members_for_cluster(session, row.id))

    def test_a_cluster_without_contradictions_is_flagged_false(
        self, session, statements
    ):
        replace_clusters(session, [_cluster(statements[:4])])

        [row] = list_clusters(session)
        assert row.contradiction_flag is False
        assert row.contradiction_score == 0.0
        assert contradictions_for_cluster(session, row.id) == []

    def test_contradiction_flag_and_score_are_attached(self, session, statements):
        pair = ContradictionPair(
            CONTRADICTION_PAIR[0], 103, CONTRADICTION_PAIR[1], 104, 0.88
        )

        replace_clusters(session, [_cluster(statements[:4], contradictions=[pair])])

        [row] = list_clusters(session)
        assert row.contradiction_flag is True
        assert row.contradiction_score == pytest.approx(0.88)

    def test_each_contradicting_pair_is_stored_for_audit(self, session, statements):
        pair = ContradictionPair(
            CONTRADICTION_PAIR[0], 103, CONTRADICTION_PAIR[1], 104, 0.88
        )

        [cluster_id] = replace_clusters(
            session, [_cluster(statements[:4], contradictions=[pair])]
        )

        [stored] = contradictions_for_cluster(session, cluster_id)
        assert stored.left_point_id == CONTRADICTION_PAIR[0]
        assert stored.right_point_id == CONTRADICTION_PAIR[1]
        assert stored.left_paper_id == 103
        assert stored.right_paper_id == 104
        assert stored.score == pytest.approx(0.88)

    def test_a_rebuild_replaces_the_previous_run_entirely(self, session, statements):
        replace_clusters(session, [_cluster(statements[:4]), _cluster(statements[4:8])])

        replace_clusters(session, [_cluster(statements[8:12], "compute cost")])

        rows = list_clusters(session)
        assert len(rows) == 1
        assert rows[0].label == "compute cost"
        assert session.scalars(select(ClusterMember)).all() != []
        assert len(session.scalars(select(ClusterMember)).all()) == 4

    def test_a_rebuild_leaves_no_orphaned_members_or_contradictions(
        self, session, statements
    ):
        pair = ContradictionPair(
            CONTRADICTION_PAIR[0], 103, CONTRADICTION_PAIR[1], 104, 0.88
        )
        replace_clusters(session, [_cluster(statements[:4], contradictions=[pair])])

        replace_clusters(session, [])

        assert session.scalars(select(Cluster)).all() == []
        assert session.scalars(select(ClusterMember)).all() == []
        assert session.scalars(select(ClusterContradiction)).all() == []

    def test_replace_flushes_but_does_not_commit(self, session, statements):
        # `routes_backend` owns the transaction and commits after the call.
        replace_clusters(session, [_cluster(statements[:4])])

        assert session.in_transaction()
        session.rollback()
        assert list_clusters(session) == []

    def test_a_null_label_is_allowed(self, session, statements):
        replace_clusters(session, [_cluster(statements[:4], label=None)])

        assert list_clusters(session)[0].label is None


class TestQueries:
    def test_clusters_are_listed_largest_first(self, session, statements):
        replace_clusters(
            session, [_cluster(statements[:3]), _cluster(statements[3:8], "bigger")]
        )

        rows = list_clusters(session)

        assert [row.member_count for row in rows] == [5, 3]

    def test_members_of_another_cluster_are_not_returned(self, session, statements):
        _first, second = replace_clusters(
            session, [_cluster(statements[:4]), _cluster(statements[4:8], "other")]
        )

        assert all(
            member.cluster_id == second
            for member in members_for_cluster(session, second)
        )

    def test_an_unknown_cluster_id_returns_nothing(self, session):
        assert members_for_cluster(session, 9999) == []
        assert contradictions_for_cluster(session, 9999) == []
