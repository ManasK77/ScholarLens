import pytest

from app.clustering.schema import (
    ContradictionPair,
    GapStatement,
    StatementCluster,
)
from tests.clustering.labelled import synthetic_gap_points


def _point(**overrides) -> dict:
    point = synthetic_gap_points()[0]
    point.update(overrides)
    return point


class TestGapStatementFromPoint:
    def test_reads_every_field_the_storage_contract_needs(self):
        statement = GapStatement.from_point(_point())

        assert statement.point_id == "p-ml-1"
        assert statement.paper_id == 101
        assert statement.year == 2023
        assert statement.source_type == "limitation"
        assert statement.text.startswith("Our evaluation is limited to English")
        assert len(statement.vector) == 8

    def test_collapses_whitespace_that_came_out_of_a_pdf(self):
        statement = GapStatement.from_point(_point(text="  low\n resource   gap "))

        assert statement.text == "low resource gap"

    def test_rejects_a_point_with_no_vector(self):
        with pytest.raises(ValueError, match="vector"):
            GapStatement.from_point(_point(vector=[]))

    def test_rejects_a_non_finite_vector(self):
        point = _point()
        point["vector"] = [float("nan")] + point["vector"][1:]

        with pytest.raises(ValueError, match="finite"):
            GapStatement.from_point(point)

    def test_rejects_blank_text(self):
        with pytest.raises(ValueError, match="text"):
            GapStatement.from_point(_point(text="   "))

    def test_rejects_a_missing_point_id(self):
        with pytest.raises(ValueError, match="point_id"):
            GapStatement.from_point(_point(id=""))

    def test_rejects_a_non_integer_paper_id(self):
        with pytest.raises(TypeError, match="paper_id"):
            GapStatement.from_point(_point(paper_id="101"))

    def test_a_missing_year_is_allowed(self):
        # A manual upload can reach Stage 5 with metadata GROBID could not
        # recover; Phase 7 treats the statement as undated rather than dropping it.
        assert GapStatement.from_point(_point(year=None)).year is None

    def test_a_non_integer_year_is_discarded_rather_than_fatal(self):
        assert GapStatement.from_point(_point(year="2024")).year is None

    def test_rejects_a_paragraph_point(self):
        # `gap_points()` filters to limitation/future_work; anything else here
        # means the filter broke, and clustering paragraphs would swamp Stage 6.
        with pytest.raises(ValueError, match="source_type"):
            GapStatement.from_point(_point(source_type="paragraph"))


class TestStatementCluster:
    def test_member_count_and_paper_ids_are_derived_from_members(self):
        statements = [
            GapStatement.from_point(point) for point in synthetic_gap_points()[:3]
        ]
        cluster = StatementCluster(label="english bias", members=statements)

        assert cluster.member_count == 3
        assert cluster.paper_ids == (101, 102, 103)

    def test_rejects_an_empty_cluster(self):
        with pytest.raises(ValueError, match="at least one member"):
            StatementCluster(label="empty", members=[])

    def test_contradiction_flag_is_false_without_pairs(self):
        statements = [
            GapStatement.from_point(point) for point in synthetic_gap_points()[:3]
        ]
        cluster = StatementCluster(label="x", members=statements)

        assert cluster.contradiction_flag is False
        assert cluster.contradiction_score == 0.0

    def test_contradiction_score_is_the_strongest_pair(self):
        statements = [
            GapStatement.from_point(point) for point in synthetic_gap_points()[:3]
        ]
        cluster = StatementCluster(
            label="x",
            members=statements,
            contradictions=[
                ContradictionPair("p-ml-1", 101, "p-ml-2", 102, 0.61),
                ContradictionPair("p-ml-1", 101, "p-ml-3", 103, 0.94),
            ],
        )

        assert cluster.contradiction_flag is True
        assert cluster.contradiction_score == pytest.approx(0.94)


class TestContradictionPair:
    def test_rejects_a_same_paper_pair(self):
        # CLUSTER-02 is about disagreement *between* papers (PROGRESS D-06-04).
        with pytest.raises(ValueError, match="different papers"):
            ContradictionPair("a", 7, "b", 7, 0.9)

    def test_rejects_a_score_outside_zero_one(self):
        with pytest.raises(ValueError, match="score"):
            ContradictionPair("a", 1, "b", 2, 1.4)

    def test_rejects_a_pair_of_one_point_with_itself(self):
        with pytest.raises(ValueError, match="distinct"):
            ContradictionPair("a", 1, "a", 2, 0.9)
