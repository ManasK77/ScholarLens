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
from collections import Counter
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clustering.claims import MIN_RESEARCH_WORDS, research_statement_rejection
from app.clustering.cluster import cluster_statements
from app.clustering.nli import get_detector
from app.clustering.schema import GapStatement, StatementCluster
from app.clustering.storage import replace_clusters
from app.core.config import Settings, get_settings
from app.core.pipeline import PaperStageState, current_run, require_complete_projections
from app.embeddings.indexer import EvidenceIndex

logger = logging.getLogger(__name__)


def load_gap_statements(
    index: EvidenceIndex, min_statement_words: int = MIN_RESEARCH_WORDS
) -> list[GapStatement]:
    """Every indexed research statement, furniture and malformed points skipped.

    Payloads originate in PDF text, so a single unusable row is a data problem,
    not a bug — it must not abandon a corpus-wide rebuild (README §13.6).

    **The furniture filter (D-06-13).** Stage 2/3 extract a paper's reference
    list, author lists, acknowledgements and headings as `limitation`
    statements, Stage 5 embeds them, and clustering them produces themes like
    "amir anastasopoulos andrew". On the 20-paper corpus that was ~11 of 27
    clusters, four of them in the projected top 10 — and they ranked *well*,
    because a cluster of author names has no on-theme solver and so loses
    nothing to `solution_density`.

    This is the right boundary for the fix, and the smallest one:

    * it is the single place Stage 6 reads its input, and the only consumer of
      `gap_points()`, so nothing else has to know;
    * it is downstream of Stages 2-5, so no PDF is re-parsed, no fact
      re-extracted and no vector re-embedded — the points stay in Qdrant
      exactly as Stage 5 wrote them, and the filter is reversible by changing
      one setting rather than by re-running an expensive stage;
    * it is upstream of *everything* derived — clusters, labels, member counts,
      Stage 7's per-year counts and Stage 8's signals all read from what this
      returns, so one filter fixes all of them at once.

    The predicate is `research_statement_rejection`, deliberately **not**
    `claim_rejection`: the latter rejects proposals, and a future-work
    statement is a proposal, so reusing it here would discard half of what
    README §8 Stage 6 exists to cluster.
    """
    statements: list[GapStatement] = []
    dropped: Counter[str] = Counter()
    run = current_run()
    eligible = (
        set(
            run.session.scalars(
                select(PaperStageState.paper_id).where(
                    PaperStageState.stage == "s5", PaperStageState.status == "done"
                )
            )
        )
        if run
        else None
    )
    for point in index.gap_points():
        if eligible is not None and point.get("paper_id") not in eligible:
            raise RuntimeError(
                "Unreconciled vector evidence; run the read-only reconciliation tool"
            )
        try:
            statement = GapStatement.from_point(point)
        except (TypeError, ValueError) as exc:
            logger.warning("Skipping unusable gap point %r: %s", point.get("id"), exc)
            continue
        # After `from_point`, so the reconciliation guard above still sees every
        # stored point and a malformed payload is still reported as malformed
        # rather than silently reclassified as furniture.
        reason = research_statement_rejection(statement.text, min_statement_words)
        if reason:
            dropped[reason] += 1
            continue
        statements.append(statement)

    if dropped:
        # A count per reason, never a line per statement: this runs over the
        # whole corpus and README §13.7 is explicit about bulky output.
        logger.info(
            "Stage 6: skipped %d point(s) that are not research statements (%s)",
            sum(dropped.values()),
            ", ".join(f"{reason}={count}" for reason, count in sorted(dropped.items())),
        )
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
            if current_run():
                raise
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
    require_complete_projections(session)
    index = index or EvidenceIndex(settings)

    started = perf_counter()
    statements = load_gap_statements(index, settings.clustering_min_statement_words)
    logger.info(
        "s6 gap_points=%d scroll_elapsed=%.3fs",
        len(statements),
        perf_counter() - started,
    )
    if not statements:
        logger.info("Stage 6: no limitation/future-work vectors indexed yet")
        return replace_clusters(session, [])

    started = perf_counter()
    clusters = cluster_statements(
        statements,
        min_cluster_size=settings.clustering_min_cluster_size,
        min_samples=settings.clustering_min_samples,
        max_statements=settings.clustering_max_statements,
    )
    clustered = sum(cluster.member_count for cluster in clusters)
    logger.info(
        "s6 hdbscan elapsed=%.3fs clusters=%d noise=%d",
        perf_counter() - started,
        len(clusters),
        len(statements) - clustered,
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
    detector = detector or get_detector()
    scored_before = getattr(detector, "pairs_scored", 0)
    started = perf_counter()
    clusters = _with_contradictions(clusters, detector)
    logger.info(
        "s6 nli elapsed=%.3fs nli_pairs_scored=%d",
        perf_counter() - started,
        getattr(detector, "pairs_scored", 0) - scored_before,
    )

    ids = replace_clusters(session, clusters)
    logger.info(
        "Stage 6: stored %d clusters over %d statements (%d flagged)",
        len(ids),
        sum(cluster.member_count for cluster in clusters),
        sum(1 for cluster in clusters if cluster.contradiction_flag),
    )
    return ids
