"""Stage 6 orchestration: gap vectors in, stored clusters out."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.clustering import service
from app.clustering.nli import CONTRADICTION, ENTAILMENT, NEUTRAL
from app.clustering.storage import (
    contradictions_for_cluster,
    list_clusters,
    members_for_cluster,
)
from app.ingestion import corpus
from tests.clustering.labelled import (
    CONTRADICTION_PAIR,
    LABELLED_STATEMENTS,
    THEMES,
    synthetic_gap_points,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    corpus.Base.metadata.create_all(engine)
    with Session(engine) as active:
        yield active


@pytest.fixture()
def settings():
    return SimpleNamespace(
        clustering_min_cluster_size=3,
        clustering_min_statement_words=6,
        clustering_min_samples=None,
        clustering_max_statements=1000,
        nli_batch_size=8,
        nli_max_chars=1000,
        nli_max_pairs_per_cluster=50,
        contradiction_threshold=0.5,
        contradiction_min_claim_words=8,
        contradiction_min_shared_terms=3,
        contradiction_min_topic_jaccard=0.12,
        contradiction_require_both_directions=True,
        contradiction_min_paper_pairs=2,
    )


class FakeIndex:
    def __init__(self, points):
        self.points = points
        self.calls = 0

    def gap_points(self):
        self.calls += 1
        return list(self.points)


def _text(point_id: str) -> str:
    return " ".join(
        next(
            item.text for item in LABELLED_STATEMENTS if item.point_id == point_id
        ).split()
    )


class FakeDetector:
    """Contradicts exactly the hand-labelled pair, in both directions.

    Both directions because D-06-12 now requires them to agree: a real
    disagreement contradicts whichever statement is taken as the premise, while
    a score in one order only is an NLI ordering artefact. `frozenset` is what
    makes the stand-in order-insensitive the way a real disagreement is.
    """

    def __init__(self):
        self.seen = 0

    def __call__(self, pairs):
        self.seen += len(pairs)
        target = frozenset((_text(CONTRADICTION_PAIR[0]), _text(CONTRADICTION_PAIR[1])))
        return [
            {CONTRADICTION: 0.91, NEUTRAL: 0.06, ENTAILMENT: 0.03}
            if frozenset(pair) == target
            else {CONTRADICTION: 0.02, NEUTRAL: 0.9, ENTAILMENT: 0.08}
            for pair in pairs
        ]


def _detector(settings, **overrides):
    """The production detector wiring, with the hand-labelled set's one caveat.

    `min_paper_pairs` defaults to 1 here rather than to the production 2. The
    labelled fixture contains exactly one disagreeing paper pair (D-06-12
    counts *papers*, and p-ml-3/p-ml-4 are the only two that disagree), so the
    production default would leave the set with nothing to flag and these tests
    would assert on an empty result either way. That the default suppresses a
    lone pair is asserted directly in
    test_the_hand_labelled_contradiction_needs_corroboration.
    """
    from app.clustering.nli import ContradictionDetector

    kwargs = {
        "threshold": settings.contradiction_threshold,
        "batch_size": settings.nli_batch_size,
        "max_chars": settings.nli_max_chars,
        "max_pairs_per_cluster": settings.nli_max_pairs_per_cluster,
        "min_claim_words": settings.contradiction_min_claim_words,
        "min_shared_terms": settings.contradiction_min_shared_terms,
        "min_topic_jaccard": settings.contradiction_min_topic_jaccard,
        "require_both_directions": settings.contradiction_require_both_directions,
        "min_paper_pairs": 1,
    }
    kwargs.update(overrides)
    return ContradictionDetector(FakeDetector(), **kwargs)


class TestRebuildClusters:
    def test_persists_one_cluster_per_labelled_theme(self, session, settings):
        ids = service.rebuild_clusters(
            session,
            settings=settings,
            index=FakeIndex(synthetic_gap_points()),
            detector=_detector(settings),
        )

        assert len(ids) == len(THEMES)
        assert [row.id for row in list_clusters(session)] == sorted(ids, key=ids.index)

    def test_members_are_stored_for_every_cluster(self, session, settings):
        ids = service.rebuild_clusters(
            session,
            settings=settings,
            index=FakeIndex(synthetic_gap_points()),
            detector=_detector(settings),
        )

        for cluster_id in ids:
            members = members_for_cluster(session, cluster_id)
            assert members
            assert all(member.text for member in members)

    def test_the_hand_labelled_contradiction_is_flagged(self, session, settings):
        service.rebuild_clusters(
            session,
            settings=settings,
            index=FakeIndex(synthetic_gap_points()),
            detector=_detector(settings),
        )

        flagged = [row for row in list_clusters(session) if row.contradiction_flag]

        assert len(flagged) == 1
        assert flagged[0].contradiction_score == pytest.approx(0.91)
        [pair] = contradictions_for_cluster(session, flagged[0].id)
        assert {pair.left_point_id, pair.right_point_id} == set(CONTRADICTION_PAIR)

    def test_the_hand_labelled_contradiction_needs_corroboration(
        self, session, settings
    ):
        # D-06-12: one disagreeing paper pair is not a cluster-level
        # disagreement. The labelled set has exactly one, so at the production
        # default of two nothing is flagged and nothing is stored -- there is
        # no conflicting evidence for the dashboard to explain.
        service.rebuild_clusters(
            session,
            settings=settings,
            index=FakeIndex(synthetic_gap_points()),
            detector=_detector(
                settings,
                min_paper_pairs=settings.contradiction_min_paper_pairs,
            ),
        )

        assert settings.contradiction_min_paper_pairs == 2
        assert [row for row in list_clusters(session) if row.contradiction_flag] == []
        assert all(
            contradictions_for_cluster(session, row.id) == []
            for row in list_clusters(session)
        )

    def test_clusters_without_disagreement_are_not_flagged(self, session, settings):
        service.rebuild_clusters(
            session,
            settings=settings,
            index=FakeIndex(synthetic_gap_points()),
            detector=_detector(settings),
        )

        unflagged = [
            row for row in list_clusters(session) if not row.contradiction_flag
        ]

        assert len(unflagged) == 2
        assert all(row.contradiction_score == 0.0 for row in unflagged)

    def test_an_empty_index_yields_no_clusters_and_loads_no_model(
        self, session, settings, monkeypatch
    ):
        monkeypatch.setattr(
            service, "get_detector", lambda *a, **k: pytest.fail("loaded NLI model")
        )

        assert (
            service.rebuild_clusters(session, settings=settings, index=FakeIndex([]))
            == []
        )
        assert list_clusters(session) == []

    def test_statements_that_form_no_cluster_load_no_model(
        self, session, settings, monkeypatch
    ):
        monkeypatch.setattr(
            service, "get_detector", lambda *a, **k: pytest.fail("loaded NLI model")
        )

        assert (
            service.rebuild_clusters(
                session, settings=settings, index=FakeIndex(synthetic_gap_points()[:2])
            )
            == []
        )

    def test_reads_the_vector_store_once(self, session, settings):
        index = FakeIndex(synthetic_gap_points())

        service.rebuild_clusters(
            session, settings=settings, index=index, detector=_detector(settings)
        )

        assert index.calls == 1

    def test_a_rebuild_replaces_the_previous_run(self, session, settings):
        first = service.rebuild_clusters(
            session,
            settings=settings,
            index=FakeIndex(synthetic_gap_points()),
            detector=_detector(settings),
        )
        second = service.rebuild_clusters(
            session,
            settings=settings,
            index=FakeIndex(synthetic_gap_points()),
            detector=_detector(settings),
        )

        # Ids are not stable across rebuilds — these tables are derived, and
        # Phases 7-8 rebuild against whatever ids this run produced.
        assert len(first) == len(second) == len(THEMES)
        assert [row.id for row in list_clusters(session)] == sorted(
            second, key=second.index
        )
        assert sum(len(members_for_cluster(session, cid)) for cid in second) == 12

    def test_does_not_commit_the_callers_transaction(self, session, settings):
        service.rebuild_clusters(
            session,
            settings=settings,
            index=FakeIndex(synthetic_gap_points()),
            detector=_detector(settings),
        )

        session.rollback()
        assert list_clusters(session) == []

    def test_a_malformed_point_is_skipped_not_fatal(self, session, settings):
        # Payloads come out of PDF text; one bad row must not abandon Stage 6.
        points = synthetic_gap_points()
        points.insert(0, {**points[0], "id": "", "text": ""})

        ids = service.rebuild_clusters(
            session,
            settings=settings,
            index=FakeIndex(points),
            detector=_detector(settings),
        )

        assert len(ids) == len(THEMES)

    def test_an_nli_failure_still_stores_the_clusters(self, session, settings):
        # A contradiction flag is one signal among five in Phase 8; losing it
        # must not cost the whole clustering run.
        class Broken:
            def detect(self, members):
                raise RuntimeError("model exploded")

        ids = service.rebuild_clusters(
            session,
            settings=settings,
            index=FakeIndex(synthetic_gap_points()),
            detector=Broken(),
        )

        assert len(ids) == len(THEMES)
        assert all(not row.contradiction_flag for row in list_clusters(session))
