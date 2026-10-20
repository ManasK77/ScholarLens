"""One sentence is one statement, however many labels Stage 3 gave it (D-06-14).

Stage 3 may type a sentence both `limitation` and `future_work`; Stage 5 then
indexes it twice, with the same vector. Clustered twice, the copy sits at
distance zero from its twin, which shrinks HDBSCAN's core distances: on the
live corpus two of six clusters (3 and 5) existed only because of such a copy,
and both were among the five reported gaps.
"""

import numpy as np

from app.clustering.cluster import (
    cluster_gap_statements,
    cluster_statements,
    collapse_duplicate_statements,
)
from app.clustering.schema import GapStatement, StatementCluster


def _statement(point_id, paper_id, text, vector, source_type="limitation"):
    return GapStatement(point_id, paper_id, text, 2024, source_type, tuple(vector))


def test_copies_of_one_sentence_collapse_and_keep_every_label():
    a_lim = _statement("p2", 1, "It fails on Tamil.", [1.0, 0.0])
    a_fw = _statement("p1", 1, "It fails on Tamil.", [1.0, 0.0], "future_work")
    other_paper = _statement("p3", 2, "It fails on Tamil.", [1.0, 0.0])

    representatives, aliases = collapse_duplicate_statements([a_lim, a_fw, other_paper])

    # The smallest point id represents the sentence; the other label is kept.
    assert [s.point_id for s in representatives] == ["p1", "p3"]
    assert aliases == {"p1": (a_lim,)}
    # The same words in another paper are another paper's evidence: kept.
    assert other_paper in representatives


def test_member_count_counts_statements_not_label_rows():
    rows = (
        _statement("p1", 1, "It fails on Tamil.", [1.0], "future_work"),
        _statement("p2", 1, "It fails on Tamil.", [1.0]),
        _statement("p3", 2, "Another limitation.", [1.0]),
    )
    assert StatementCluster(label="x", members=rows).member_count == 2


# Two tight themes and scattered noise (seeded, 16 dimensions). Theme B has only
# two distinct sentences; its third row is a second label on one of them.
_rng = np.random.default_rng(1)
_A_CENTRE, _B_CENTRE = _rng.normal(size=16), _rng.normal(size=16)
THEME_A = [
    _statement(
        f"a{i}",
        10 + i,
        f"Theme A limitation {i}.",
        _A_CENTRE + 0.05 * _rng.normal(size=16),
    )
    for i in range(4)
]
THEME_B = [
    _statement(
        f"b{i}",
        20 + i,
        f"Theme B limitation {i}.",
        _B_CENTRE + 0.05 * _rng.normal(size=16),
    )
    for i in range(2)
]
THEME_B_ALIAS = _statement(
    "b9", 20, "Theme B limitation 0.", THEME_B[0].vector, "future_work"
)
NOISE = [
    _statement(f"n{i}", 30 + i, f"Unrelated sentence {i}.", _rng.normal(size=16))
    for i in range(10)
]
THEME_A_ALIAS = _statement(
    "a9", 10, "Theme A limitation 0.", THEME_A[0].vector, "future_work"
)
WITH_B_COPY = [*THEME_A, *THEME_B, THEME_B_ALIAS, *NOISE]
STATEMENTS = [*WITH_B_COPY, THEME_A_ALIAS]
B_TEXTS = {s.text for s in THEME_B}


def _texts(cluster):
    return {member.text for member in cluster.members}


def test_precondition_a_label_copy_alone_can_create_a_cluster():
    """Clustering label rows (the old behaviour) turns two sentences into a theme."""
    clusters = cluster_statements(WITH_B_COPY, min_cluster_size=3)
    assert any(_texts(c) == B_TEXTS for c in clusters)


def test_each_sentence_is_clustered_once():
    clusters = cluster_gap_statements(STATEMENTS, min_cluster_size=3)

    # Two distinct sentences are not a theme of their own at min_cluster_size=3.
    assert not any(_texts(c) == B_TEXTS for c in clusters)
    [theme_a] = [c for c in clusters if THEME_A_ALIAS.text in _texts(c)]
    assert theme_a.member_count == len(_texts(theme_a))
    assert len(theme_a.members) == theme_a.member_count + 1
    # Both labels of the doubly-typed sentence are still stored with the theme.
    assert {m.source_type for m in theme_a.members if m.text == THEME_A_ALIAS.text} == {
        "limitation",
        "future_work",
    }


def test_clustering_ignores_label_copies_entirely():
    """Same partition and labels as clustering the distinct sentences alone."""
    deduped = cluster_gap_statements(STATEMENTS, min_cluster_size=3)
    distinct = cluster_statements(
        collapse_duplicate_statements(STATEMENTS)[0], min_cluster_size=3
    )
    assert [(c.label, c.member_count, _texts(c)) for c in deduped] == [
        (c.label, c.member_count, _texts(c)) for c in distinct
    ]
