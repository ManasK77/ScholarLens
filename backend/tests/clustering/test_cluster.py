"""Clustering behaviour, driven by the hand-labelled set in `labelled.py`."""

import math

import pytest

from app.clustering import cluster as cluster_module
from app.clustering.cluster import (
    center_vectors,
    cluster_statements,
    label_for_statements,
    normalize_vectors,
)
from app.clustering.schema import GapStatement
from tests.clustering.labelled import (
    LABELLED_STATEMENTS,
    THEMES,
    statements_for_theme,
    synthetic_gap_points,
)


@pytest.fixture
def statements() -> list[GapStatement]:
    return [GapStatement.from_point(point) for point in synthetic_gap_points()]


def _theme_of(point_id: str) -> str:
    return next(item.theme for item in LABELLED_STATEMENTS if item.point_id == point_id)


class TestNormalizeVectors:
    def test_every_row_becomes_unit_length(self):
        rows = normalize_vectors([[3.0, 4.0], [0.0, 2.0]])

        for row in rows:
            assert math.isclose(math.sqrt(sum(v * v for v in row)), 1.0, rel_tol=1e-6)

    def test_a_zero_vector_is_left_alone_rather_than_dividing_by_zero(self):
        assert normalize_vectors([[0.0, 0.0]]) == [[0.0, 0.0]]

    def test_direction_is_preserved(self):
        [row] = normalize_vectors([[3.0, 4.0]])

        assert row[0] == pytest.approx(0.6)
        assert row[1] == pytest.approx(0.8)


class TestCenterVectors:
    """D-06-08: the anisotropy correction that makes SPECTER2 vectors separable."""

    def test_each_column_ends_up_mean_zero(self):
        rows = center_vectors([[1.0, 10.0], [3.0, 20.0], [5.0, 30.0]])

        for index in range(2):
            assert sum(row[index] for row in rows) == pytest.approx(0.0)

    def test_relative_geometry_is_preserved(self):
        rows = center_vectors([[1.0, 0.0], [3.0, 0.0]])

        assert rows[1][0] - rows[0][0] == pytest.approx(2.0)

    def test_it_separates_what_raw_cosine_cannot(self, statements):
        # The measured failure: two groups offset along a shared dominant
        # direction are near-identical under raw cosine and obvious once the
        # shared direction is removed.
        raw = [[10.0, 1.0, 0.0], [10.0, 0.9, 0.1], [10.0, 0.0, 1.0]]

        def cosine(a, b):
            dot = sum(x * y for x, y in zip(a, b, strict=True))
            return dot / (
                math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
            )

        before = cosine(raw[0], raw[2])
        centered = center_vectors(raw)
        after = cosine(centered[0], centered[2])

        assert before > 0.98
        assert after < before

    def test_an_empty_batch_is_returned_unchanged(self):
        assert center_vectors([]) == []

    def test_a_single_vector_centres_to_the_origin(self):
        assert center_vectors([[2.0, -3.0]]) == [[0.0, 0.0]]


class TestClusterStatements:
    """CLUSTER-01: the hand-labelled themes must come back as clusters."""

    def test_each_labelled_theme_becomes_exactly_one_cluster(self, statements):
        clusters = cluster_statements(statements, min_cluster_size=3)

        recovered = [
            frozenset(_theme_of(member.point_id) for member in cluster.members)
            for cluster in clusters
        ]
        assert len(clusters) == len(THEMES)
        assert sorted(recovered, key=sorted) == sorted(
            (frozenset({theme}) for theme in THEMES), key=sorted
        )

    def test_every_member_of_a_cluster_shares_one_theme(self, statements):
        for cluster in cluster_statements(statements, min_cluster_size=3):
            themes = {_theme_of(member.point_id) for member in cluster.members}
            assert len(themes) == 1
            assert themes.pop() in THEMES

    def test_all_four_multilingual_statements_land_together(self, statements):
        clusters = cluster_statements(statements, min_cluster_size=3)

        [multilingual] = [
            cluster
            for cluster in clusters
            if _theme_of(cluster.members[0].point_id) == "multilingual"
        ]
        assert {member.point_id for member in multilingual.members} == {
            item.point_id for item in statements_for_theme("multilingual")
        }

    def test_points_hdbscan_calls_noise_are_dropped(self, statements, monkeypatch):
        """D-06-07: `-1` never becomes a cluster.

        A bucket of leftovers would earn a mention count, a trend slope and a
        gap score in Phases 7-8, manufacturing a gap out of noise. Whether a
        given point *is* noise is HDBSCAN's density judgement, not this
        module's, so the label array is fixed here and only our handling of it
        is under test.
        """
        labels = [0, 0, 0, -1, 1, 1, 1, -1, 2, 2, 2, -1]

        class FixedHDBSCAN:
            def __init__(self, **kwargs):
                pass

            def fit_predict(self, matrix):
                assert len(matrix) == len(labels)
                return labels

        monkeypatch.setattr(cluster_module, "HDBSCAN", FixedHDBSCAN)

        clusters = cluster_statements(statements, min_cluster_size=3)

        clustered = {
            member.point_id for cluster in clusters for member in cluster.members
        }
        noise = {
            statement.point_id
            for statement, label in zip(statements, labels, strict=True)
            if label == -1
        }
        assert len(clusters) == 3
        assert all(cluster.member_count == 3 for cluster in clusters)
        assert clustered.isdisjoint(noise)

    def test_no_cluster_count_is_supplied(self, statements):
        # CLUSTER-01 requires clustering without a pre-specified count: the only
        # knobs are density parameters, and the result is discovered.
        clusters = cluster_statements(statements, min_cluster_size=3)

        assert len(clusters) == 3

    def test_clusters_are_ordered_largest_first_and_deterministically(self, statements):
        first = cluster_statements(statements, min_cluster_size=3)
        second = cluster_statements(statements, min_cluster_size=3)

        sizes = [cluster.member_count for cluster in first]
        assert sizes == sorted(sizes, reverse=True)
        assert [
            [member.point_id for member in cluster.members] for cluster in first
        ] == [[member.point_id for member in cluster.members] for cluster in second]

    def test_members_are_ordered_by_point_id(self, statements):
        for cluster in cluster_statements(statements, min_cluster_size=3):
            ids = [member.point_id for member in cluster.members]
            assert ids == sorted(ids)

    def test_every_cluster_gets_a_label(self, statements):
        for cluster in cluster_statements(statements, min_cluster_size=3):
            assert cluster.label
            assert cluster.label.strip() == cluster.label

    def test_too_few_statements_to_form_a_cluster_yields_nothing(self, statements):
        assert cluster_statements(statements[:2], min_cluster_size=3) == []

    def test_ragged_vectors_are_rejected(self, statements):
        # Mixing widths means two different embedding models reached one
        # collection; centring them together would be meaningless.
        ragged = [
            statements[0],
            statements[1],
            GapStatement(
                statements[2].point_id,
                statements[2].paper_id,
                statements[2].text,
                statements[2].year,
                statements[2].source_type,
                statements[2].vector[:4],
            ),
        ]

        with pytest.raises(ValueError, match="ragged"):
            cluster_statements(ragged, min_cluster_size=3)

    def test_no_statements_yields_nothing(self):
        assert cluster_statements([], min_cluster_size=3) == []

    def test_min_cluster_size_below_two_is_rejected(self, statements):
        # HDBSCAN's own floor. 1 would make every point its own "cluster".
        with pytest.raises(ValueError, match="min_cluster_size"):
            cluster_statements(statements, min_cluster_size=1)

    def test_the_capped_subset_does_not_depend_on_arrival_order(self, statements):
        # `gap_points()` returns whatever order Qdrant scrolled in. If the cap
        # ever bites, two runs over the same corpus must still cluster the same
        # statements — otherwise the stored clusters silently change identity.
        forward = cluster_statements(statements, min_cluster_size=3, max_statements=8)
        reversed_order = cluster_statements(
            list(reversed(statements)), min_cluster_size=3, max_statements=8
        )

        assert forward, "the cap left nothing to compare"
        assert sorted(
            member.point_id for cluster in forward for member in cluster.members
        ) == sorted(
            member.point_id for cluster in reversed_order for member in cluster.members
        )

    def test_max_statements_caps_the_work(self, statements):
        # README 13.6: the corpus decides how many vectors arrive, so the cap
        # is what stops an O(n^2) distance matrix from sizing itself.
        clusters = cluster_statements(statements, min_cluster_size=3, max_statements=4)

        clustered = [member for cluster in clusters for member in cluster.members]
        assert len(clustered) <= 4


class TestLabelForStatements:
    def test_label_reflects_the_shared_vocabulary(self, statements):
        multilingual = [
            statement
            for statement in statements
            if _theme_of(statement.point_id) == "multilingual"
        ]

        label = label_for_statements(multilingual)

        assert "languages" in label or "language" in label

    def test_label_drops_stopwords_and_generic_hedges(self, statements):
        label = label_for_statements(
            [
                statement
                for statement in statements
                if _theme_of(statement.point_id) == "compute"
            ]
        )

        assert "the" not in label.split()
        assert "future" not in label.split()
        assert "work" not in label.split()

    def test_label_is_deterministic(self, statements):
        subset = statements[:4]

        assert label_for_statements(subset) == label_for_statements(subset)

    def test_label_is_bounded_in_length(self, statements):
        assert len(label_for_statements(statements).split()) <= 4

    def test_statements_with_no_usable_terms_get_no_label(self):
        empties = [
            GapStatement.from_point(point) for point in synthetic_gap_points()[:2]
        ]
        stripped = [
            GapStatement(
                statement.point_id,
                statement.paper_id,
                "the of and to",
                statement.year,
                statement.source_type,
                statement.vector,
            )
            for statement in empties
        ]

        assert label_for_statements(stripped) is None
