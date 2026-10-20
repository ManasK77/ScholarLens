"""A Stage 6 rebuild keeps reports whose generation inputs did not change.

Rebuilding clusters gives every cluster a new id and clears its report. A report
is a function of the cluster label, the member texts its retrieval query is
built from, the member papers evidence selection prefers, and the five Stage 8
signals in its prompt (plus settings and the index, unchanged by a rebuild).
When all of those are identical, regenerating would only spend an LLM call to
get a differently-worded copy of a valid report.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.clustering.schema import GapStatement, StatementCluster
from app.clustering.storage import list_clusters, replace_clusters
from app.explanation.service import carry_over_reports, report_inputs
from app.explanation.storage import GapReport, list_reports
from app.ingestion import corpus
from app.ranking.signals import GapSignals
from app.ranking.storage import replace_rankings


def _cluster(label, texts):
    return StatementCluster(
        label=label,
        members=[
            GapStatement(f"p{i}", 100 + i, text, 2024, "limitation", (0.1,))
            for i, text in enumerate(texts)
        ],
    )


def _store(session, clusters, signals):
    ids = replace_clusters(session, clusters)
    replace_rankings(
        session, [(cid, 1.0, sig) for cid, sig in zip(ids, signals, strict=True)]
    )
    return ids


def _report(cluster_id, gap):
    return GapReport(
        cluster_id=cluster_id,
        gap=gap,
        evidence=[{"id": "e", "paper_id": 100, "text": "t"}],
        confidence=0.5,
        suggested_directions=["d"],
        provider="groq",
        model="m",
        justification={},
    )


SIG = GapSignals(2, 0.5, 0.0, 1.0, 0.0)


def test_only_reports_with_identical_inputs_are_carried_to_the_new_ids():
    engine = create_engine("sqlite://")
    corpus.Base.metadata.create_all(engine)
    with Session(engine) as session:
        old = _store(
            session,
            [
                _cluster("same", ["a", "b"]),
                _cluster("relabelled", ["c", "d"]),
                _cluster("resized", ["e", "f"]),
            ],
            [SIG, SIG, SIG],
        )
        session.add_all(
            [
                _report(old[0], "kept"),
                _report(old[1], "stale label"),
                _report(old[2], "stale members"),
            ]
        )
        session.flush()
        snapshot = report_inputs(session)

        new = _store(
            session,
            [
                _cluster("same", ["a", "b"]),
                _cluster("other label", ["c", "d"]),
                _cluster("resized", ["e", "f", "g"]),
            ],
            [SIG, SIG, SIG],
        )
        assert list_reports(session) == []  # the rebuild cleared them

        carried = carry_over_reports(session, snapshot)

        assert carried == [new[0]]
        [row] = list_reports(session)
        assert (row.cluster_id, row.gap) == (new[0], "kept")


def test_a_changed_signal_means_the_report_is_regenerated():
    engine = create_engine("sqlite://")
    corpus.Base.metadata.create_all(engine)
    with Session(engine) as session:
        [old] = _store(session, [_cluster("same", ["a", "b"])], [SIG])
        session.add(_report(old, "kept"))
        session.flush()
        snapshot = report_inputs(session)
        _store(
            session, [_cluster("same", ["a", "b"])], [GapSignals(3, 0.5, 0.0, 1.0, 0.0)]
        )

        assert carry_over_reports(session, snapshot) == []


def test_a_second_label_row_does_not_change_a_reports_inputs():
    engine = create_engine("sqlite://")
    corpus.Base.metadata.create_all(engine)
    with Session(engine) as session:
        [old] = _store(session, [_cluster("same", ["a", "b"])], [SIG])
        session.add(_report(old, "kept"))
        session.flush()
        snapshot = report_inputs(session)
        with_label_copy = StatementCluster(
            label="same",
            members=[
                *_cluster("same", ["a", "b"]).members,
                GapStatement("p9", 100, "a", 2024, "future_work", (0.1,)),
            ],
        )
        new = _store(session, [with_label_copy], [SIG])

        assert carry_over_reports(session, snapshot) == new
        assert list_clusters(session)[0].member_count == 2
