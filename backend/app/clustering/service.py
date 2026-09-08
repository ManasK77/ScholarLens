"""Stage 6 orchestration — gap vectors in, stored clusters out.

The whole phase in one pass:

1. read every `limitation` / `future_work` point with its SPECTER2 vector out
   of Qdrant (Phase 5's `gap_points()`),
2. cluster them by embedding similarity with **no fixed cluster count**
   (CLUSTER-01),
3. run pairwise DeBERTa-MNLI inside each cluster to flag cross-paper
   disagreement (CLUSTER-02),
4. replace the stored clusters, members and contradiction flags (CLUSTER-03).

Two properties this module owns:

* **Nothing is loaded speculatively.** An empty index, or a corpus whose
  statements form no cluster, returns `[]` without ever constructing the NLI
  model — the same laziness `get_encoder()` gives Phase 5.
* **One failure does not abandon the run.** A malformed Qdrant payload is
  skipped, and an NLI failure costs that cluster its contradiction flag (one of
  five Phase 8 signals), not the clustering result.
"""

import logging

from sqlalchemy.orm import Session

from app.clustering.cluster import cluster_statements
from app.clustering.nli import get_detector
from app.clustering.schema import GapStatement, StatementCluster
from app.clustering.storage import replace_clusters
from app.core.config import Settings, get_settings
from app.embeddings.indexer import EvidenceIndex

logger = logging.getLogger(__name__)


def load_gap_statements(index: EvidenceIndex) -> list[GapStatement]:
    """Every indexed limitation/future-work statement, malformed points skipped.

    Payloads originate in PDF text, so a single unusable row is a data problem,
    not a bug — it must not abandon a corpus-wide rebuild (README §13.6).
    """
    statements: list[GapStatement] = []
    for point in index.gap_points():
        try:
            statements.append(GapStatement.from_point(point))
        except (TypeError, ValueError) as exc:
            logger.warning("Skipping unusable gap point %r: %s", point.get("id"), exc)
    return statements


def _with_contradictions(
    clusters: list[StatementCluster], detector
) -> list[StatementCluster]:
    """Attach cross-paper contradiction flags, one cluster at a time."""
    flagged = []
    for cluster in clusters:
        try:
            contradictions = detector.detect(cluster.members)
        except Exception:
            # The flag is one of five signals in Phase 8's gap score; losing it
            # for one cluster must not cost the whole Stage 6 result.
            logger.exception(
                "Contradiction detection failed for cluster %r; storing it unflagged",
                cluster.label,
            )
            contradictions = []
        flagged.append(
            StatementCluster(
                label=cluster.label,
                members=cluster.members,
                contradictions=contradictions,
            )
        )
    return flagged


def rebuild_clusters(
    session: Session,
    settings: Settings | None = None,
    index: EvidenceIndex | None = None,
    detector=None,
) -> list[int]:
    """Recompute and store every cluster. Returns the new cluster ids.

    The caller owns the transaction (`routes_backend` commits afterwards), so
    this flushes and never commits.
    """
    settings = settings or get_settings()
    index = index or EvidenceIndex(settings)

    statements = load_gap_statements(index)
    if not statements:
        logger.info("Stage 6: no limitation/future-work vectors indexed yet")
        return replace_clusters(session, [])

    clusters = cluster_statements(
        statements,
        min_cluster_size=settings.clustering_min_cluster_size,
        min_samples=settings.clustering_min_samples,
        max_statements=settings.clustering_max_statements,
    )
    if not clusters:
        logger.info(
            "Stage 6: %d gap statements formed no cluster at min_cluster_size=%d",
            len(statements),
            settings.clustering_min_cluster_size,
        )
        return replace_clusters(session, [])

    # Resolved only now: a corpus with nothing to compare never pays for a
    # DeBERTa-MNLI load.
    clusters = _with_contradictions(clusters, detector or get_detector())

    ids = replace_clusters(session, clusters)
    logger.info(
        "Stage 6: stored %d clusters over %d statements (%d flagged)",
        len(ids),
        sum(cluster.member_count for cluster in clusters),
        sum(1 for cluster in clusters if cluster.contradiction_flag),
    )
    return ids
