"""Stage 6 against the real models — the check that the semantics hold.

The fast unit tests drive `cluster_statements` with synthetic vectors, so they
prove the *wiring*: no fixed cluster count, noise dropped, members grouped,
order stable. Only this test proves the thing README §15 Phase 6 actually asks
for — that the hand-labelled limitation sentences cluster together *when
embedded by SPECTER2*, and that DeBERTa-MNLI flags the deliberate cross-paper
disagreement in the multilingual theme.

Excluded from the default run (`-m "not integration"`): it downloads and runs
two real checkpoints. Run from the repo root, where `.env` lives:

    python -m pytest backend/tests/clustering/test_integration.py -m integration
"""

import pytest

from app.clustering.cluster import cluster_statements
from app.clustering.schema import GapStatement
from tests.clustering.labelled import (
    CONTRADICTION_PAIR,
    LABELLED_STATEMENTS,
    THEMES,
    statements_for_theme,
)

pytestmark = pytest.mark.integration


def _theme_of(point_id: str) -> str:
    return next(item.theme for item in LABELLED_STATEMENTS if item.point_id == point_id)


@pytest.fixture(scope="module")
def specter2_statements() -> list[GapStatement]:
    """The hand-labelled set embedded by the real SPECTER2 encoder."""
    from app.embeddings.encoder import get_encoder

    encoder = get_encoder()
    vectors = encoder.encode([item.text for item in LABELLED_STATEMENTS], 8)
    return [
        GapStatement(
            item.point_id,
            item.paper_id,
            " ".join(item.text.split()),
            item.year,
            "future_work" if item.text.startswith("Future work") else "limitation",
            tuple(vector),
        )
        for item, vector in zip(LABELLED_STATEMENTS, vectors, strict=True)
    ]


def test_specter2_embeddings_recover_the_hand_labelled_themes(specter2_statements):
    """CLUSTER-01, on real embeddings and with no cluster count supplied."""
    clusters = cluster_statements(specter2_statements, min_cluster_size=3)

    assert clusters, "SPECTER2 embeddings produced no clusters at all"
    for cluster in clusters:
        themes = {_theme_of(member.point_id) for member in cluster.members}
        assert len(themes) == 1, (
            "a cluster mixed hand-labelled themes: "
            f"{[(m.point_id, _theme_of(m.point_id)) for m in cluster.members]}"
        )

    recovered = {_theme_of(cluster.members[0].point_id) for cluster in clusters}
    assert recovered == set(THEMES), f"themes not recovered: {sorted(recovered)}"


def test_the_multilingual_theme_is_recovered_whole(specter2_statements):
    clusters = cluster_statements(specter2_statements, min_cluster_size=3)

    [multilingual] = [
        cluster
        for cluster in clusters
        if _theme_of(cluster.members[0].point_id) == "multilingual"
    ]
    assert {member.point_id for member in multilingual.members} == {
        item.point_id for item in statements_for_theme("multilingual")
    }


def test_clusters_get_a_readable_label(specter2_statements):
    for cluster in cluster_statements(specter2_statements, min_cluster_size=3):
        assert cluster.label


def test_deberta_mnli_flags_the_hand_labelled_contradiction(specter2_statements):
    """CLUSTER-02, on the real NLI checkpoint."""
    from app.clustering.nli import ContradictionDetector, load_deberta_mnli
    from app.core.config import get_settings

    settings = get_settings()
    detector = ContradictionDetector(
        load_deberta_mnli(settings),
        threshold=settings.contradiction_threshold,
        batch_size=settings.nli_batch_size,
        max_chars=settings.nli_max_chars,
        max_pairs_per_cluster=settings.nli_max_pairs_per_cluster,
    )

    multilingual = [
        statement
        for statement in specter2_statements
        if _theme_of(statement.point_id) == "multilingual"
    ]
    found = detector.detect(multilingual)

    flagged = {frozenset((pair.left_point_id, pair.right_point_id)) for pair in found}
    assert frozenset(CONTRADICTION_PAIR) in flagged, (
        "the deliberate cross-paper disagreement was not flagged; "
        f"flagged pairs: {[tuple(pair) for pair in flagged]}"
    )


def test_agreeing_statements_in_a_theme_are_not_flagged(specter2_statements):
    """The compute-cost theme contains no disagreement and must stay unflagged."""
    from app.clustering.nli import ContradictionDetector, load_deberta_mnli
    from app.core.config import get_settings

    settings = get_settings()
    detector = ContradictionDetector(
        load_deberta_mnli(settings),
        threshold=settings.contradiction_threshold,
        batch_size=settings.nli_batch_size,
        max_chars=settings.nli_max_chars,
        max_pairs_per_cluster=settings.nli_max_pairs_per_cluster,
    )

    compute = [
        statement
        for statement in specter2_statements
        if _theme_of(statement.point_id) == "compute"
    ]

    assert detector.detect(compute) == []
