"""PostgreSQL/SQLAlchemy persistence for Stage 7 trends (TREND-02).

Like Stage 6's tables this one is **derived**: everything in it is recomputed
from `clusters` + `cluster_members` by `service.rebuild_trends`, so a rebuild
replaces the previous run wholesale rather than merging into it.

`app/api/routes_backend.py` is the frozen consumer contract: `list_trends` rows
expose `.cluster_id`, `.yearly_counts` and `.slope`, and `GET /trends`
serializes those three straight into its response.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, Float, ForeignKey, delete, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.ingestion.corpus import Base

if TYPE_CHECKING:  # pragma: no cover -- `analysis` costs a Pandas import
    # `app/main.py` imports this module only to put `cluster_trends` on
    # `Base.metadata`, so paying ~0.6s of Pandas import at application startup
    # for one annotation is not a trade worth making. `replace_trends` only
    # ever reads `.yearly_counts` and `.slope` off the value.
    from app.trends.analysis import TrendSeries


class ClusterTrend(Base):
    """One cluster's mention time series and its trend slope.

    A separate table rather than columns on `clusters`, because Phase 6 rewrites
    `clusters` wholesale on every rebuild and cluster ids are not stable across
    runs — a trend has to be recomputed after clustering, not carried along with
    it. `ON DELETE CASCADE` is what stops a bare `POST /clusters` failing on a
    foreign key against PostgreSQL once a trend row exists (D-07-07).
    """

    __tablename__ = "cluster_trends"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Unique: exactly one trend per cluster, so `GET /trends` can never show a
    # cluster twice with two different slopes.
    cluster_id: Mapped[int] = mapped_column(
        ForeignKey("clusters.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    # `{"2021": 4, "2022": 0, "2023": 9}` — year strings, ascending, interior
    # gaps zero-filled. Keys are strings because this dict is served as JSON and
    # `frontend/src/types.ts` declares `Record<string, number>`; storing int
    # keys would come back as strings anyway and make the round trip lossy.
    yearly_counts: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Signed mentions per year. Phase 8 multiplies this by `w2`.
    slope: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


def replace_trends(
    session: Session, trends: list[tuple[int, "TrendSeries"]]
) -> list[int]:
    """Atomically replace every stored trend within the caller's transaction.

    Returns the cluster ids stored, in the order given — `POST /trends` returns
    them to the caller under the key `clusters`.

    Flushes but never commits: `routes_backend` owns the transaction and commits
    after the call.

    The wipe is unconditional and explicit rather than relying on the foreign
    key's `ON DELETE CASCADE`, because SQLite does not enforce foreign keys by
    default — a Phase 6 rebuild there leaves rows pointing at cluster ids that
    no longer mean anything, and they must not survive into the next `/trends`
    payload.
    """
    session.execute(delete(ClusterTrend))

    session.add_all(
        ClusterTrend(
            cluster_id=cluster_id,
            yearly_counts=dict(series.yearly_counts),
            slope=series.slope,
        )
        for cluster_id, series in trends
    )
    session.flush()
    return [cluster_id for cluster_id, _ in trends]


def list_trends(session: Session) -> list[ClusterTrend]:
    """Every stored trend, ordered by cluster id so the payload is stable.

    The dashboard does its own sorting (by slope, and by absolute slope for the
    chart), so this only has to be deterministic.
    """
    return list(session.scalars(select(ClusterTrend).order_by(ClusterTrend.cluster_id)))
