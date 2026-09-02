from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clustering.storage import ClusterMember, SemanticCluster
from app.trends.analysis import compute_trend
from app.trends.storage import ClusterTrend


def rebuild_trends(session: Session) -> list[int]:
    processed = []
    cluster_ids = session.scalars(
        select(SemanticCluster.id).order_by(SemanticCluster.id)
    )
    for cluster_id in cluster_ids:
        years = list(
            session.scalars(
                select(ClusterMember.year).where(
                    ClusterMember.cluster_id == cluster_id,
                    ClusterMember.year.is_not(None),
                )
            )
        )
        result = compute_trend(years)
        row = session.get(ClusterTrend, cluster_id)
        if row is None:
            row = ClusterTrend(cluster_id=cluster_id, yearly_counts={}, slope=0.0)
            session.add(row)
        row.yearly_counts = result.yearly_counts
        row.slope = result.slope
        processed.append(cluster_id)
    session.flush()
    return processed
