"""PostgreSQL/SQLAlchemy persistence for Stage 8 gap rankings (GAPRANK-02).

Like Stage 6's and Stage 7's tables this one is **derived**: everything in it is
recomputed from `clusters` + `cluster_members` + `cluster_trends` + the corpus
by `service.rebuild_rankings`, so a rebuild replaces the previous run wholesale
rather than merging into it.

`app/api/routes_backend.py` is the frozen consumer contract: `list_rankings`
rows expose `.cluster_id`, `.score` and the five signal columns, and `GET /gaps`
serializes the five under a `signals` object. The dashboard numbers the rows it
receives `index + 1`, so the ordering here *is* the rank.

Every signal is stored beside the score on purpose. A gap the system reports
has to be checkable by hand — `27 * w1 + 2.5 * w2 - 0.2 * w3 + ...` — which is
the difference between a transparent formula and an opaque one.
"""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    delete,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.ingestion.corpus import Base
from app.ranking.signals import GapSignals


class ClusterRanking(Base):
    """One cluster's gap score and the five signals it was computed from.

    A separate table rather than columns on `clusters`, for the same reason
    `cluster_trends` is one: Phase 6 rewrites `clusters` wholesale on every
    rebuild and cluster ids are not stable across runs. The `ForeignKey` is what
    makes `replace_clusters._dependent_tables()` discover this table and clear
    it, so a ranking can never end up attached to a recycled id.
    """

    __tablename__ = "cluster_rankings"

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        ForeignKey("clusters.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    # README §8's `gap_score`, rounded (D-08-08). Can be negative: a theme whose
    # solution density outweighs its other signals is ranked last, not dropped.
    score: Mapped[float] = mapped_column(Float, nullable=False)

    # --- the breakdown, in README §8's formula order ---
    # Distinct papers mentioning the theme (D-08-02).
    mention_frequency: Mapped[int] = mapped_column(Integer, nullable=False)
    # Stage 7's signed mentions-per-year slope, copied unscaled (D-08-03).
    trend_slope: Mapped[float] = mapped_column(Float, nullable=False)
    # Papers proposing solutions / papers mentioning the problem, in [0, 1].
    solution_density: Mapped[float] = mapped_column(Float, nullable=False)
    # Mean citation count of the mentioning papers (D-08-05).
    citation_impact: Mapped[float] = mapped_column(Float, nullable=False)
    # 0.0 / 1.0, not a boolean: `frontend/src/types.ts` types it `number`.
    contradiction_flag: Mapped[float] = mapped_column(Float, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


def replace_rankings(
    session: Session, rankings: list[tuple[int, float, GapSignals]]
) -> list[int]:
    """Atomically replace every stored ranking within the caller's transaction.

    Takes `(cluster_id, score, signals)` and returns the cluster ids in the
    order given — `POST /gaps` returns them to the caller under the key
    `clusters`.

    Flushes but never commits: `routes_backend` owns the transaction and commits
    after the call.

    The wipe is unconditional and explicit rather than relying on the foreign
    key's `ON DELETE CASCADE`, because SQLite — the documented local-dev store —
    does not enforce foreign keys by default.
    """
    session.execute(delete(ClusterRanking))

    session.add_all(
        ClusterRanking(
            cluster_id=cluster_id,
            score=score,
            mention_frequency=signals.mention_frequency,
            trend_slope=signals.trend_slope,
            solution_density=signals.solution_density,
            citation_impact=signals.citation_impact,
            contradiction_flag=signals.contradiction_flag,
        )
        for cluster_id, score, signals in rankings
    )
    session.flush()
    return [cluster_id for cluster_id, _, _ in rankings]


def list_rankings(session: Session, limit: int) -> list[ClusterRanking]:
    """The ranked gaps, best first, capped at `limit`.

    Highest score first, ties broken by cluster id (D-08-09): the dashboard
    prints the row order as the rank, so it must not depend on which order the
    rows happened to be inserted in.
    """
    return list(
        session.scalars(
            select(ClusterRanking)
            .order_by(ClusterRanking.score.desc(), ClusterRanking.cluster_id)
            .limit(limit)
        )
    )
