"""Stage 7 orchestration — persisted clusters in, persisted trends out.

The whole phase in one pass:

1. read every stored Phase 6 cluster and its members,
2. aggregate each cluster's members into distinct papers per publication year
   (TREND-01),
3. fit a trend slope over that series (TREND-02),
4. replace the stored trends.

Two properties this module owns:

* **It is entirely offline.** No model, no vector store, no HTTP call, no clock:
  its input is two PostgreSQL tables Phase 6 already wrote, and running it twice
  over the same rows produces byte-identical output.
* **Every cluster gets a row.** A cluster whose members are all undated is
  stored with an empty series and a `0.0` slope rather than skipped, so Phase 8
  never has to distinguish "no trend" from "not computed" (D-07-07).
"""

import logging

from sqlalchemy.orm import Session

from app.clustering.storage import list_clusters, members_for_cluster
from app.core.config import Settings, get_settings
from app.trends.analysis import analyze_cluster
from app.trends.storage import replace_trends

logger = logging.getLogger(__name__)


def rebuild_trends(session: Session, settings: Settings | None = None) -> list[int]:
    """Recompute and store every cluster's trend. Returns the cluster ids.

    The caller owns the transaction (`routes_backend` commits afterwards), so
    this flushes and never commits.
    """
    settings = settings or get_settings()

    clusters = list_clusters(session)
    if not clusters:
        logger.info("Stage 7: no clusters stored yet")
        return replace_trends(session, [])

    computed = []
    undated = 0
    for cluster in clusters:
        # Read through Phase 6's published API rather than its tables, so Stage
        # 7 stays behind the same boundary `/clusters` uses.
        members = members_for_cluster(session, cluster.id)
        series = analyze_cluster(
            ((member.paper_id, member.year) for member in members),
            min_year=settings.trend_min_year,
            max_year=settings.trend_max_year,
        )
        undated += series.undated_mentions
        computed.append((cluster.id, series))

    ids = replace_trends(session, computed)
    if undated:
        # Not an error: a manual upload GROBID could not date has no year to
        # put on a time axis (D-07-03). Worth seeing, because a corpus where
        # this number is large has a metadata problem upstream, not a trend one.
        logger.info("Stage 7: %d cluster members carried no usable year", undated)
    logger.info(
        "Stage 7: stored %d trends (%d with a non-zero slope)",
        len(ids),
        sum(1 for _, series in computed if series.slope != 0.0),
    )
    return ids
