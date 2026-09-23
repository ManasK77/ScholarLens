"""The S6 clustering input boundary drops document furniture (D-06-13).

`load_gap_statements` is the one place Stage 6 reads its input, and the only
consumer of `EvidenceIndex.gap_points()`. Filtering here rather than in Stage 2
or 5 means nothing is re-parsed, re-extracted or re-embedded: the vectors stay
in Qdrant exactly as Stage 5 wrote them, and a statement that stops being
clustered can start again by changing one setting.
"""

import logging

import pytest

from app.clustering.service import load_gap_statements

REFERENCE = (
    "In Findings of the Association for Computational Linguistics: EMNLP 2023, "
    "pages 12365-12394, Singapore."
)
AUTHORS = "Claire Cui, Olivier Bousquet, Quoc V. Le, and Ed H. Chi."
FUNDING = (
    "(MAPG) and the PaliGemma Academic Program award from Google for her work "
    "in AI safety alignment."
)
HEADING = "DISCUSSION AND LIMITATIONS"
YEAR = "2023."

LIMITATION = (
    "The variation in vulnerabilities across different prompt designs "
    "underscores the limitations of current defenses and the need for "
    "adaptive safeguards."
)
FUTURE_WORK = (
    "Future work includes extending this analysis to additional conferences "
    "and LLMs, and investigating mitigation techniques to improve robustness."
)


def _point(point_id: str, text: str, paper_id: int = 101, source_type="limitation"):
    return {
        "id": point_id,
        "paper_id": paper_id,
        "text": text,
        "year": 2024,
        "source_type": source_type,
        "vector": [0.1, 0.2],
    }


class FakeIndex:
    def __init__(self, points):
        self.points = points

    def gap_points(self):
        return list(self.points)


MIXED = [
    _point("ref", REFERENCE),
    _point("authors", AUTHORS),
    _point("funding", FUNDING),
    _point("heading", HEADING),
    _point("year", YEAR),
    _point("limitation", LIMITATION),
    _point("future", FUTURE_WORK, source_type="future_work"),
]


class TestFurnitureNeverReachesClustering:
    def test_only_research_statements_survive(self) -> None:
        kept = load_gap_statements(FakeIndex(MIXED))

        assert [item.point_id for item in kept] == ["limitation", "future"]

    @pytest.mark.parametrize(
        "point_id", ["ref", "authors", "funding", "heading", "year"]
    )
    def test_each_kind_of_furniture_is_dropped(self, point_id: str) -> None:
        kept = load_gap_statements(FakeIndex(MIXED))

        assert point_id not in {item.point_id for item in kept}

    def test_future_work_is_not_treated_as_a_proposal_here(self) -> None:
        # The distinction that stops this filter being `claim_rejection`.
        kept = load_gap_statements(
            FakeIndex([_point("f", FUTURE_WORK, source_type="future_work")])
        )

        assert len(kept) == 1

    def test_an_all_furniture_index_yields_nothing_rather_than_raising(self) -> None:
        assert load_gap_statements(FakeIndex([_point("ref", REFERENCE)])) == []

    def test_an_empty_index_is_unchanged(self) -> None:
        assert load_gap_statements(FakeIndex([])) == []


class TestBoundaryContract:
    def test_the_floor_is_injectable_so_a_corpus_can_loosen_it(self) -> None:
        short = _point("short", "Multilingual coverage remains poor.")

        assert load_gap_statements(FakeIndex([short]), min_statement_words=3)
        assert load_gap_statements(FakeIndex([short]), min_statement_words=20) == []

    def test_a_malformed_point_is_still_skipped_not_fatal(self) -> None:
        # The pre-existing contract: one bad payload must not abandon a
        # corpus-wide rebuild. Filtering must not have turned that into a raise.
        bad = {"id": "bad", "paper_id": "not-an-int", "text": LIMITATION}

        kept = load_gap_statements(FakeIndex([bad, _point("ok", LIMITATION)]))

        assert [item.point_id for item in kept] == ["ok"]

    def test_what_was_dropped_is_logged_as_a_summary(self, caplog) -> None:
        # Context hygiene: a count per reason, not one line per statement.
        with caplog.at_level(logging.INFO, logger="app.clustering.service"):
            load_gap_statements(FakeIndex(MIXED))

        [record] = [r for r in caplog.records if "not research statements" in r.message]
        assert "bibliography" in record.message
        assert "author_list" in record.message

    def test_nothing_is_logged_when_nothing_is_dropped(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="app.clustering.service"):
            load_gap_statements(FakeIndex([_point("ok", LIMITATION)]))

        assert not [r for r in caplog.records if "not research statements" in r.message]
