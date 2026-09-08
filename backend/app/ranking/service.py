"""Stage 8 orchestration — persisted clusters/trends/facts in, ranked gaps out.

The whole phase in one pass:

1. read every stored Phase 6 cluster and its members,
2. read Phase 7's stored slope per cluster,
3. derive the five README §8 signals per cluster from those rows plus the
   corpus (`papers.citation_count`) and Phase 3's `contribution` facts,
4. combine them with the configured weights and replace the stored rankings.

Three properties this module owns:

* **It is entirely offline.** No model, no vector store, no HTTP call, no clock:
  its input is four PostgreSQL tables earlier phases already wrote, and running
  it twice over the same rows produces identical output. That is what makes the
  project's "the same evidence produces the same ranking" claim testable.
* **Every cluster gets a row.** A cluster that scores `0.0` scored zero; it was
  not skipped. Nothing downstream has to tell "no gap" from "not computed".
* **Nothing branches on a paper's `source`** (CLAUDE.md hard constraint 5).

`signals.py` owns the arithmetic; this module owns only the reads.
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clustering.storage import list_clusters, members_for_cluster
from app.core.config import Settings, get_settings
from app.extraction.schema import FactType
from app.extraction.storage import ExtractedFact
from app.ingestion.corpus import Paper
from app.ranking.signals import (
    GapSignals,
    citation_impact,
    contradiction_signal,
    gap_score,
    mention_frequency,
    solution_density,
    tokenize_contributions,
    trend_slope_signal,
)
from app.ranking.storage import replace_rankings
from app.trends.storage import list_trends

logger = logging.getLogger(__name__)


def _citation_counts(session: Session) -> dict[int, int | None]:
    """`paper_id -> citation_count` for the whole corpus.

    Only Semantic Scholar reports one, so most values are `None` on a corpus
    collected from arXiv or ACL alone — `citation_impact` reads that as no
    evidence of impact (D-08-05).
    """
    return {
        paper_id: count
        for paper_id, count in session.execute(
            select(Paper.id, Paper.citation_count)
        ).all()
    }


def _contributions_by_paper(session: Session) -> dict[int, list[str]]:
    """`paper_id -> contribution statements`, corpus-wide (D-08-04).

    Corpus-wide rather than per cluster on purpose: a paper that solved a
    problem rarely lists it as its own limitation, so it is not a cluster member
    and restricting the numerator to members would leave `solution_density` at
    zero for every theme.

    Two columns rather than whole ORM rows — this is the one query in the phase
    whose size grows with the corpus, and nothing here needs the other nine
    columns.
    """
    contributions: dict[int, list[str]] = {}
    rows = session.execute(
        select(ExtractedFact.paper_id, ExtractedFact.text).where(
            ExtractedFact.fact_type == FactType.CONTRIBUTION.value
        )
    ).all()
    for paper_id, text in rows:
        contributions.setdefault(paper_id, []).append(text)
    return contributions


def rebuild_rankings(session: Session, settings: Settings | None = None) -> list[int]:
    """Recompute and store every cluster's gap score. Returns the cluster ids.

    The caller owns the transaction (`routes_backend` commits afterwards), so
    this flushes and never commits.
    """
    settings = settings or get_settings()

    clusters = list_clusters(session)
    if not clusters:
        logger.info("Stage 8: no clusters stored yet")
        return replace_rankings(session, [])

    weights = settings.gap_weights()
    slopes = {row.cluster_id: row.slope for row in list_trends(session)}
    citations = _citation_counts(session)
    # Tokenized once for the whole run: `solution_density` is called per
    # cluster against the same corpus-wide contributions.
    contributions = tokenize_contributions(_contributions_by_paper(session))

    computed = []
    for cluster in clusters:
        # Read through Phase 6's published API rather than its tables, so
        # Stage 8 stays behind the same boundary `/clusters` uses.
        paper_ids = [
            member.paper_id for member in members_for_cluster(session, cluster.id)
        ]
        mentions = mention_frequency(paper_ids)
        signals = GapSignals(
            mention_frequency=mentions,
            trend_slope=trend_slope_signal(slopes.get(cluster.id)),
            solution_density=solution_density(cluster.label, mentions, contributions),
            # `ClusterMember.paper_id` carries no foreign key by Phase 6's
            # design — the vector store can lag the corpus — so a member whose
            # paper is gone contributes an unknown count, not a KeyError.
            citation_impact=citation_impact(
                citations.get(paper_id) for paper_id in set(paper_ids)
            ),
            contradiction_flag=contradiction_signal(cluster.contradiction_flag),
        )
        computed.append((cluster.id, gap_score(signals, weights), signals))

    # Stored best-first is not enough on its own: `list_rankings` orders the
    # read as well, because insertion order is not a ranking.
    computed.sort(key=lambda item: (-item[1], item[0]))

    ids = replace_rankings(session, computed)
    logger.info(
        "Stage 8: ranked %d clusters (top score %.4f)",
        len(ids),
        computed[0][1],
    )
    return ids
