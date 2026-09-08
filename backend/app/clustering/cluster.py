"""HDBSCAN over SPECTER2 vectors (CLUSTER-01).

**No cluster count is ever supplied.** HDBSCAN discovers how many themes exist
from the density of the embedding space; the only knobs are `min_cluster_size`
and `min_samples`. That is the whole reason README §7 names it: the number of
distinct limitation themes in a corpus is exactly what Stage 6 is trying to
find out, so it cannot be an input.

The implementation is `sklearn.cluster.HDBSCAN` — HDBSCAN proper, ported
in-tree in scikit-learn 1.3 — rather than the standalone `hdbscan` package,
which needs a C++/Cython build (PROGRESS.md D-06-01).

`numpy` and `sklearn` are imported at module scope here because this module is
*only* imported by the clustering service; nothing in the API import path
reaches it without intending to cluster.
"""

import logging
import math
import re
from collections import Counter
from collections.abc import Sequence

import numpy as np
from sklearn.cluster import HDBSCAN

from app.clustering.schema import GapStatement, StatementCluster

logger = logging.getLogger(__name__)

# HDBSCAN's own floor: a "cluster" of one is a point, and Phases 7-8 would give
# it a trend slope and a gap score.
MIN_CLUSTER_SIZE_FLOOR = 2

# Points HDBSCAN could not place. D-06-07: dropped, never swept into a
# catch-all cluster.
NOISE_LABEL = -1

_WORD = re.compile(r"[a-z][a-z0-9-]{2,}")

# Deliberately narrow: English function words plus the hedging vocabulary every
# limitation/future-work sentence shares, which would otherwise label every
# cluster "future work limitations".
_STOPWORD_TEXT = """
    a about above after again against all also although among an and any are as at
    be because been before being below between both but by can cannot could did do
    does doing done due during each either else etc even every few for from further
    had has have having her here hers him his how however i if in into is it its
    itself just may might more most much must my no nor not now of off on once only
    or other others ought our ours out over own per rather same she should since so
    some some such than that the their theirs them themselves then there these they
    this those though through thus to too under until up upon use used using very
    was we were what when where whether which while who whom why will with within
    without would yet you your yours
    approach approaches current currently datasets evaluation existing experiment
    experiments future large limit limitation limitations limited method methods
    model models paper papers possible potential propose proposed remain remains
    research result results section study studies suggest task tasks whereas work
    works
    """
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())

# A label is a display affordance, not evidence (D-06-06). Long enough to be
# recognisable in the dashboard, short enough to fit a chart legend.
_MAX_LABEL_TERMS = 3


def normalize_vectors(vectors: Sequence[Sequence[float]]) -> list[list[float]]:
    """L2-normalize each row, leaving a zero row untouched.

    D-06-02: Qdrant scores this collection with cosine distance, so clustering
    must agree with retrieval. On unit vectors `euclidean² = 2 − 2·cosine`, a
    strictly monotone transform — so euclidean HDBSCAN over normalized vectors
    yields exactly the cosine neighbourhood ordering, without depending on
    metric support in HDBSCAN's tree backends.
    """
    rows = []
    for vector in vectors:
        norm = math.sqrt(sum(float(value) * float(value) for value in vector))
        if norm == 0.0:
            rows.append([float(value) for value in vector])
        else:
            rows.append([float(value) / norm for value in vector])
    return rows


def center_vectors(vectors: Sequence[Sequence[float]]) -> list[list[float]]:
    """Subtract the batch mean from every row (D-06-08).

    Raw transformer `[CLS]` vectors are strongly anisotropic — they occupy a
    narrow cone, so almost everything is cosine-similar to almost everything
    else. Measured on the hand-labelled Stage 6 set with real SPECTER2 vectors:

        raw       within-theme cosine 0.931 (min 0.893) · across 0.871 (max 0.925)
        centered  within-theme cosine 0.354 (min 0.164) · across -0.182 (max 0.356)

    Untreated, the two distributions overlap and HDBSCAN's density estimate has
    almost nothing to work with; centered, they separate cleanly. Removing the
    corpus-mean direction is the standard correction and costs one pass.

    This shifts only the copy Stage 6 clusters. The vectors stored in Qdrant —
    which Phases 9 and 10 retrieve against — are untouched.
    """
    rows = [[float(value) for value in vector] for vector in vectors]
    if not rows:
        return rows
    width = len(rows[0])
    count = len(rows)
    mean = [sum(row[index] for row in rows) / count for index in range(width)]
    return [[value - mean[index] for index, value in enumerate(row)] for row in rows]


def label_for_statements(statements: Sequence[GapStatement]) -> str | None:
    """A short, deterministic label derived from the members' shared vocabulary.

    Terms are ranked by how many *distinct* statements mention them, so one
    verbose member cannot name the cluster on its own. Ties break
    alphabetically, which is what makes the label reproducible across runs.
    Returns `None` when nothing informative survives — the surviving
    `Cluster.label` contract is nullable and the dashboard falls back to
    `Cluster {id}`.
    """
    document_frequency: Counter[str] = Counter()
    for statement in statements:
        terms = {
            term
            for term in _WORD.findall(statement.text.lower())
            if term not in _STOPWORDS
        }
        document_frequency.update(terms)
    if not document_frequency:
        return None
    ranked = sorted(document_frequency.items(), key=lambda item: (-item[1], item[0]))
    chosen = [term for term, _ in ranked[:_MAX_LABEL_TERMS]]
    return " ".join(chosen) or None


def cluster_statements(
    statements: Sequence[GapStatement],
    min_cluster_size: int,
    min_samples: int | None = None,
    max_statements: int | None = None,
) -> list[StatementCluster]:
    """Group `statements` into themes. Returns `[]` when none form a cluster.

    `max_statements` is a hard resource guard (README §13.6): HDBSCAN's mutual
    reachability step is O(n²) in memory, and the corpus — not this code —
    decides how many gap vectors arrive.
    """
    if min_cluster_size < MIN_CLUSTER_SIZE_FLOOR:
        raise ValueError(
            f"min_cluster_size must be at least {MIN_CLUSTER_SIZE_FLOOR}, "
            f"got {min_cluster_size}"
        )

    statements = list(statements)
    if max_statements is not None:
        if max_statements <= 0:
            raise ValueError(f"max_statements must be positive, got {max_statements}")
        if len(statements) > max_statements:
            logger.warning(
                "Stage 6 received %d gap statements; clustering the first %d "
                "by point id (CLUSTERING_MAX_STATEMENTS)",
                len(statements),
                max_statements,
            )
            # Sorted before truncating: `gap_points()` returns whatever order
            # Qdrant scrolled in, so slicing the arrival order would let two
            # runs over the same corpus cluster different statements.
            statements = sorted(statements, key=lambda item: item.point_id)
            statements = statements[:max_statements]

    if len(statements) < min_cluster_size:
        return []

    vectors = [statement.vector for statement in statements]
    if len({len(vector) for vector in vectors}) != 1:
        raise ValueError("gap vectors are ragged; every point must share a dimension")

    # Centre first, then L2-normalize: centring is what makes the themes
    # separable at all (D-06-08), normalizing is what makes euclidean HDBSCAN
    # equivalent to cosine (D-06-02).
    matrix = np.asarray(
        normalize_vectors(center_vectors(vectors)),
        dtype=np.float64,
    )

    model = HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric="euclidean",
        allow_single_cluster=False,
        # `matrix` is built here and used nowhere else, so letting HDBSCAN work
        # in place saves a second copy of the whole corpus. Passed explicitly
        # because the scikit-learn default is changing.
        copy=False,
    )
    labels = model.fit_predict(matrix)

    grouped: dict[int, list[GapStatement]] = {}
    for statement, label in zip(statements, labels, strict=True):
        label = int(label)
        if label == NOISE_LABEL:
            continue
        grouped.setdefault(label, []).append(statement)

    clusters = [
        StatementCluster(
            label=label_for_statements(members),
            members=sorted(members, key=lambda member: member.point_id),
        )
        for _, members in sorted(grouped.items())
    ]
    # Largest first, then by the smallest member id — a stable order the stored
    # primary keys inherit, so `/clusters` does not reshuffle between rebuilds
    # for reasons a reader cannot see.
    clusters.sort(
        key=lambda cluster: (-cluster.member_count, cluster.members[0].point_id)
    )
    return clusters
