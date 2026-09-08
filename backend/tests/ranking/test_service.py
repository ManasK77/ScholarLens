"""Stage 8 orchestration: persisted Phase 3/6/7 rows in, persisted rankings out.

Everything here runs against a real SQLAlchemy session with real Phase 6 rows
written through `replace_clusters` and real Phase 7 rows written through
`replace_trends`, so the boundary between the phases is exercised rather than
mocked. The phase is offline and deterministic — no model, no vector store, no
socket, no clock.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.clustering.schema import ContradictionPair, GapStatement, StatementCluster
from app.clustering.storage import replace_clusters
from app.extraction.schema import ExtractedStatement, FactType
from app.extraction.storage import replace_facts
from app.ingestion.corpus import Base, Paper
from app.ranking.service import rebuild_rankings
from app.ranking.storage import list_rankings
from app.trends.analysis import TrendSeries
from app.trends.storage import replace_trends

WEIGHTS = {
    "gap_weight_mention_frequency": 1.0,
    "gap_weight_trend_slope": 3.0,
    "gap_weight_solution_density": 25.0,
    "gap_weight_citation_impact": 0.02,
    "gap_weight_contradiction": 5.0,
}


class FakeSettings:
    """Only `gap_weights()` is read; a real Settings needs the whole environment."""

    def __init__(self, **overrides) -> None:
        self._weights = {**WEIGHTS, **overrides}

    def gap_weights(self):
        return (
            self._weights["gap_weight_mention_frequency"],
            self._weights["gap_weight_trend_slope"],
            self._weights["gap_weight_solution_density"],
            self._weights["gap_weight_citation_impact"],
            self._weights["gap_weight_contradiction"],
        )


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


@pytest.fixture()
def settings() -> FakeSettings:
    return FakeSettings()


def _paper(session, paper_id: int, year: int | None = 2023, citations=None) -> None:
    session.add(
        Paper(
            id=paper_id,
            title=f"Paper {paper_id}",
            year=year,
            source="arxiv",
            citation_count=citations,
        )
    )
    session.flush()


def _statement(paper_id: int, text: str, year: int | None = 2023) -> GapStatement:
    return GapStatement(
        point_id=f"p{paper_id}-{abs(hash(text)) % 10_000}",
        paper_id=paper_id,
        text=text,
        year=year,
        source_type="limitation",
        vector=[0.0, 1.0],
    )


def _contribution(session, paper_id: int, text: str) -> None:
    replace_facts(
        session,
        paper_id,
        [
            ExtractedStatement(
                fact_type=FactType.CONTRIBUTION,
                text=text,
                section_type="Introduction",
                section_heading="Introduction",
                section_index=0,
                paragraph_index=0,
                sentence_index=0,
                confidence=0.9,
                extraction_method="rule",
            )
        ],
    )


def _cluster(label: str, statements, contradictions=()) -> StatementCluster:
    return StatementCluster(
        label=label, members=list(statements), contradictions=list(contradictions)
    )


def test_an_empty_corpus_stores_nothing_and_returns_nothing(session, settings) -> None:
    assert rebuild_rankings(session, settings) == []
    assert list_rankings(session, 10) == []


def test_every_cluster_gets_a_ranking(session, settings) -> None:
    """A cluster that scores zero scored zero — it was not skipped."""
    for paper_id in (1, 2, 3, 4):
        _paper(session, paper_id)
    ids = replace_clusters(
        session,
        [
            _cluster("multilingual", [_statement(1, "Poor multilingual coverage.")]),
            _cluster("latency", [_statement(3, "Inference latency is high.")]),
        ],
    )
    replace_trends(session, [(cluster_id, TrendSeries()) for cluster_id in ids])

    assert sorted(rebuild_rankings(session, settings)) == sorted(ids)
    assert len(list_rankings(session, 10)) == 2


def test_mention_frequency_counts_distinct_papers(session, settings) -> None:
    """Two statements from one paper are one mention (D-08-02)."""
    _paper(session, 1)
    _paper(session, 2)
    ids = replace_clusters(
        session,
        [
            _cluster(
                "multilingual",
                [
                    _statement(1, "Poor multilingual coverage."),
                    _statement(1, "Multilingual evaluation is missing."),
                    _statement(2, "English-only multilingual benchmarks."),
                ],
            )
        ],
    )
    replace_trends(session, [(ids[0], TrendSeries())])

    rebuild_rankings(session, settings)

    assert list_rankings(session, 10)[0].mention_frequency == 2


def test_the_trend_slope_comes_from_the_stored_phase_7_row(session, settings) -> None:
    _paper(session, 1)
    ids = replace_clusters(
        session, [_cluster("multilingual", [_statement(1, "Poor coverage.")])]
    )
    replace_trends(session, [(ids[0], TrendSeries({"2021": 1, "2022": 3}, 2.0, 0))])

    rebuild_rankings(session, settings)

    assert list_rankings(session, 10)[0].trend_slope == 2.0


def test_a_cluster_with_no_stored_trend_scores_a_zero_slope(session, settings) -> None:
    """Defensive: D-07-07 stores a row per cluster, but a bare rebuild order
    must not raise."""
    _paper(session, 1)
    replace_clusters(
        session, [_cluster("multilingual", [_statement(1, "Poor coverage.")])]
    )

    rebuild_rankings(session, settings)

    assert list_rankings(session, 10)[0].trend_slope == 0.0


def test_solution_density_finds_solvers_outside_the_cluster(session, settings) -> None:
    """A paper that solved the problem rarely lists it as its own limitation."""
    for paper_id in (1, 2, 3):
        _paper(session, paper_id)
    _contribution(session, 3, "We release a multilingual injection prompt benchmark.")
    ids = replace_clusters(
        session,
        [
            _cluster(
                "multilingual injection prompt",
                [
                    _statement(1, "Multilingual prompt injection is unstudied."),
                    _statement(2, "Prompt injection multilingual coverage is poor."),
                ],
            )
        ],
    )
    replace_trends(session, [(ids[0], TrendSeries())])

    rebuild_rankings(session, settings)

    assert list_rankings(session, 10)[0].solution_density == 0.5


def test_an_unrelated_contribution_is_not_a_solution(session, settings) -> None:
    for paper_id in (1, 2):
        _paper(session, paper_id)
    _contribution(session, 2, "We contribute a faster attention kernel.")
    ids = replace_clusters(
        session,
        [_cluster("multilingual", [_statement(1, "Poor multilingual coverage.")])],
    )
    replace_trends(session, [(ids[0], TrendSeries())])

    rebuild_rankings(session, settings)

    assert list_rankings(session, 10)[0].solution_density == 0.0


def test_only_contribution_facts_count_as_solutions(session, settings) -> None:
    """A limitation naming the theme is the problem, not a fix for it."""
    for paper_id in (1, 2):
        _paper(session, paper_id)
    replace_facts(
        session,
        2,
        [
            ExtractedStatement(
                fact_type=FactType.LIMITATION,
                text="Multilingual coverage remains poor.",
                section_type="Limitations",
                section_heading="Limitations",
                section_index=0,
                paragraph_index=0,
                sentence_index=0,
                confidence=0.9,
                extraction_method="rule",
            )
        ],
    )
    ids = replace_clusters(
        session,
        [_cluster("multilingual", [_statement(1, "Poor multilingual coverage.")])],
    )
    replace_trends(session, [(ids[0], TrendSeries())])

    rebuild_rankings(session, settings)

    assert list_rankings(session, 10)[0].solution_density == 0.0


def test_citation_impact_averages_the_mentioning_papers(session, settings) -> None:
    _paper(session, 1, citations=10)
    _paper(session, 2, citations=30)
    _paper(session, 3, citations=1000)  # not in the cluster
    ids = replace_clusters(
        session,
        [
            _cluster(
                "multilingual",
                [_statement(1, "Poor coverage."), _statement(2, "English only.")],
            )
        ],
    )
    replace_trends(session, [(ids[0], TrendSeries())])

    rebuild_rankings(session, settings)

    assert list_rankings(session, 10)[0].citation_impact == 20.0


def test_a_member_whose_paper_left_the_corpus_counts_as_uncited(
    session, settings
) -> None:
    """`ClusterMember.paper_id` carries no foreign key by Phase 6's design —
    the vector store can lag the corpus, and one stale point must not abort a
    corpus-wide rebuild."""
    _paper(session, 1, citations=10)
    ids = replace_clusters(
        session,
        [
            _cluster(
                "multilingual",
                [_statement(1, "Poor coverage."), _statement(999, "English only.")],
            )
        ],
    )
    replace_trends(session, [(ids[0], TrendSeries())])

    rebuild_rankings(session, settings)

    row = list_rankings(session, 10)[0]
    assert row.mention_frequency == 2
    assert row.citation_impact == 5.0


def test_the_contradiction_flag_comes_from_phase_6(session, settings) -> None:
    _paper(session, 1)
    _paper(session, 2)
    members = [_statement(1, "Defences work."), _statement(2, "Defences fail.")]
    ids = replace_clusters(
        session,
        [
            _cluster(
                "defences",
                members,
                [
                    ContradictionPair(
                        left_point_id=members[0].point_id,
                        left_paper_id=1,
                        right_point_id=members[1].point_id,
                        right_paper_id=2,
                        score=0.95,
                    )
                ],
            )
        ],
    )
    replace_trends(session, [(ids[0], TrendSeries())])

    rebuild_rankings(session, settings)

    assert list_rankings(session, 10)[0].contradiction_flag == 1.0


def test_the_score_is_the_weighted_sum_of_the_stored_signals(session, settings) -> None:
    """The whole point of a transparent formula: recompute it by hand."""
    _paper(session, 1, citations=100)
    _paper(session, 2, citations=200)
    ids = replace_clusters(
        session,
        [
            _cluster(
                "multilingual",
                [_statement(1, "Poor coverage."), _statement(2, "English only.")],
            )
        ],
    )
    replace_trends(session, [(ids[0], TrendSeries({"2022": 1, "2023": 2}, 1.0, 0))])

    rebuild_rankings(session, settings)

    row = list_rankings(session, 10)[0]
    assert row.score == pytest.approx(
        1.0 * row.mention_frequency
        + 3.0 * row.trend_slope
        - 25.0 * row.solution_density
        + 0.02 * row.citation_impact
        + 5.0 * row.contradiction_flag
    )


def test_the_weights_are_honoured(session, settings) -> None:
    _paper(session, 1)
    _paper(session, 2)
    ids = replace_clusters(
        session,
        [
            _cluster(
                "multilingual",
                [_statement(1, "Poor coverage."), _statement(2, "English only.")],
            )
        ],
    )
    replace_trends(session, [(ids[0], TrendSeries())])

    rebuild_rankings(session, FakeSettings(gap_weight_mention_frequency=10.0))

    assert list_rankings(session, 10)[0].score == 20.0


def test_rankings_are_stored_best_first(session, settings) -> None:
    for paper_id in range(1, 6):
        _paper(session, paper_id)
    ids = replace_clusters(
        session,
        [
            _cluster("small", [_statement(1, "Poor coverage.")]),
            _cluster(
                "big",
                [
                    _statement(2, "Poor coverage."),
                    _statement(3, "Poor coverage."),
                    _statement(4, "Poor coverage."),
                ],
            ),
        ],
    )
    replace_trends(session, [(cluster_id, TrendSeries()) for cluster_id in ids])

    rebuild_rankings(session, settings)

    assert [row.cluster_id for row in list_rankings(session, 10)] == [ids[1], ids[0]]


def test_rebuilding_twice_produces_identical_rankings(session, settings) -> None:
    """Offline and deterministic: no model, no clock, no vector store."""
    _paper(session, 1, citations=7)
    _paper(session, 2, citations=3)
    ids = replace_clusters(
        session,
        [
            _cluster(
                "multilingual",
                [_statement(1, "Poor coverage."), _statement(2, "English only.")],
            )
        ],
    )
    replace_trends(session, [(ids[0], TrendSeries({"2021": 1, "2022": 2}, 1.0, 0))])

    rebuild_rankings(session, settings)
    first = [(row.cluster_id, row.score) for row in list_rankings(session, 10)]
    rebuild_rankings(session, settings)

    assert [(row.cluster_id, row.score) for row in list_rankings(session, 10)] == first


def test_rebuild_flushes_but_never_commits(session, settings) -> None:
    _paper(session, 1)
    ids = replace_clusters(
        session, [_cluster("multilingual", [_statement(1, "Poor coverage.")])]
    )
    replace_trends(session, [(ids[0], TrendSeries())])

    rebuild_rankings(session, settings)
    session.rollback()

    assert list_rankings(session, 10) == []


def test_a_stale_ranking_does_not_survive_a_re_cluster(session, settings) -> None:
    _paper(session, 1)
    ids = replace_clusters(
        session, [_cluster("multilingual", [_statement(1, "Poor coverage.")])]
    )
    replace_trends(session, [(ids[0], TrendSeries())])
    rebuild_rankings(session, settings)

    replace_clusters(session, [])

    assert list_rankings(session, 10) == []
