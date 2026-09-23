"""Stage 9 evidence selection (D-09-04).

The shape this fixes is on record. For the 20-paper corpus's top-ranked gap —
a cluster of 18 statements from 6 papers — Stage 9 retrieved 8 passages and 7
of them came from one paper, with the eighth from a paper that was not in the
cluster at all. Five of the six mentioning papers were never quoted, and the
report's headline numbers ("56% of tests resulted in successful prompt
injections") were one study's own experiment presented as the field's position.

Nothing here is about *finding* evidence; Qdrant still ranks. It is about which
of the ranked passages a single report is allowed to rest on.
"""

import pytest

from app.explanation.selection import evidence_summary, select_evidence


def passage(point_id: str, paper_id: int) -> dict:
    return {
        "id": point_id,
        "paper_id": paper_id,
        "title": f"Paper {paper_id}",
        "text": f"passage {point_id}",
        "year": 2024,
        "source_type": "paragraph",
    }


# Qdrant's real ranking for cluster 1, by paper: seven from paper 5, one from
# paper 3, in that order.
CLUSTER_ONE_RANKING = [
    passage("e1", 5),
    passage("e2", 5),
    passage("e3", 5),
    passage("e4", 5),
    passage("e5", 5),
    passage("e6", 3),
    passage("e7", 5),
    passage("e8", 5),
]
# The cluster's own six papers. Paper 3 is not among them.
CLUSTER_ONE_PAPERS = {2, 4, 5, 7, 9, 10}


class TestPerPaperCap:
    def test_one_paper_cannot_supply_every_passage(self) -> None:
        selected = select_evidence(
            CLUSTER_ONE_RANKING, CLUSTER_ONE_PAPERS, limit=8, per_paper_cap=3
        )

        from_paper_five = [item for item in selected if item["paper_id"] == 5]
        assert len(from_paper_five) == 3

    def test_the_cap_keeps_the_highest_ranked_passages_of_that_paper(self) -> None:
        selected = select_evidence(
            CLUSTER_ONE_RANKING, CLUSTER_ONE_PAPERS, limit=8, per_paper_cap=2
        )

        assert [item["id"] for item in selected if item["paper_id"] == 5] == [
            "e1",
            "e2",
        ]

    def test_a_cap_of_one_gives_one_passage_per_paper(self) -> None:
        pool = [passage("a", 1), passage("b", 1), passage("c", 2), passage("d", 3)]

        selected = select_evidence(pool, {1, 2, 3}, limit=8, per_paper_cap=1)

        assert [item["id"] for item in selected] == ["a", "c", "d"]


class TestClusterPaperPreference:
    def test_a_papers_in_the_cluster_are_preferred_over_outsiders(self) -> None:
        pool = [
            passage("outsider-1", 3),
            passage("outsider-2", 3),
            passage("member", 9),
        ]

        selected = select_evidence(pool, {9}, limit=2, per_paper_cap=2)

        # Both of paper 3's passages outrank paper 9's, but the last slot goes
        # to the cluster's own paper rather than to a second outsider passage.
        # (The result is returned in retrieval order, so this is about which
        # passages are present, not where they sit.)
        assert {item["id"] for item in selected} == {"outsider-1", "member"}

    def test_an_outsider_still_fills_a_slot_nothing_else_wants(self) -> None:
        # Preference, not exclusion: a passage from outside the cluster is
        # still real retrieved evidence, and a thin cluster needs it.
        pool = [passage("outsider", 3), passage("member", 9)]

        selected = select_evidence(pool, {9}, limit=8, per_paper_cap=3)

        assert {item["paper_id"] for item in selected} == {3, 9}

    def test_the_real_cluster_one_ranking_gains_papers(self) -> None:
        selected = select_evidence(
            CLUSTER_ONE_RANKING, CLUSTER_ONE_PAPERS, limit=8, per_paper_cap=3
        )
        summary = evidence_summary(selected)

        # Was 7-of-8 from one paper; the cap is what bounds it.
        assert summary["max_passages_from_one_paper"] <= 3
        assert summary["unique_paper_count"] >= 2


class TestDiversityOrdering:
    def test_unique_papers_are_taken_before_second_passages(self) -> None:
        pool = [
            passage("a1", 1),
            passage("a2", 1),
            passage("a3", 1),
            passage("b1", 2),
            passage("c1", 3),
        ]

        selected = select_evidence(pool, {1, 2, 3}, limit=3, per_paper_cap=3)

        assert {item["paper_id"] for item in selected} == {1, 2, 3}

    def test_a_papers_first_passage_ranks_it(self) -> None:
        # Paper 2's best passage outranks paper 1's, so paper 2 goes first.
        pool = [passage("b1", 2), passage("a1", 1)]

        selected = select_evidence(pool, {1, 2}, limit=2, per_paper_cap=1)

        assert [item["paper_id"] for item in selected] == [2, 1]


class TestContract:
    def test_the_limit_is_respected(self) -> None:
        pool = [passage(f"p{index}", index) for index in range(20)]

        assert len(select_evidence(pool, set(), limit=5, per_paper_cap=3)) == 5

    def test_selection_is_deterministic(self) -> None:
        first = select_evidence(
            CLUSTER_ONE_RANKING, CLUSTER_ONE_PAPERS, limit=6, per_paper_cap=2
        )
        second = select_evidence(
            CLUSTER_ONE_RANKING, CLUSTER_ONE_PAPERS, limit=6, per_paper_cap=2
        )

        assert [item["id"] for item in first] == [item["id"] for item in second]

    def test_nothing_is_invented_or_mutated(self) -> None:
        selected = select_evidence(
            CLUSTER_ONE_RANKING, CLUSTER_ONE_PAPERS, limit=8, per_paper_cap=3
        )

        assert all(item in CLUSTER_ONE_RANKING for item in selected)

    def test_an_empty_pool_selects_nothing(self) -> None:
        assert select_evidence([], {1}, limit=8, per_paper_cap=3) == []

    def test_a_passage_with_no_paper_id_is_dropped(self) -> None:
        pool = [{"id": "x", "text": "orphan"}, passage("ok", 1)]

        assert [item["id"] for item in select_evidence(pool, {1}, 8, 3)] == ["ok"]

    @pytest.mark.parametrize(("limit", "cap"), [(0, 3), (8, 0), (-1, 3)])
    def test_non_positive_bounds_are_rejected(self, limit: int, cap: int) -> None:
        with pytest.raises(ValueError):
            select_evidence(CLUSTER_ONE_RANKING, {5}, limit, cap)


class TestEvidenceSummary:
    def test_it_separates_passages_from_papers(self) -> None:
        summary = evidence_summary(CLUSTER_ONE_RANKING)

        assert summary["passage_count"] == 8
        assert summary["unique_paper_count"] == 2
        assert summary["max_passages_from_one_paper"] == 7

    def test_an_empty_report_summarizes_to_zeroes(self) -> None:
        assert evidence_summary([]) == {
            "passage_count": 0,
            "unique_paper_count": 0,
            "max_passages_from_one_paper": 0,
        }

    def test_the_summary_is_json_serializable_ints(self) -> None:
        summary = evidence_summary(CLUSTER_ONE_RANKING)

        assert all(isinstance(value, int) for value in summary.values())
