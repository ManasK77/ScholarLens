"""PostgreSQL/SQLAlchemy persistence for Stage 6 clusters (CLUSTER-03).

Three tables, all **derived**: everything here is recomputed from the corpus
and the vector store by `service.rebuild_clusters`, so a rebuild replaces the
previous run wholesale rather than merging into it. That is why cluster ids are
not stable across rebuilds, and why Phases 7 and 8 rebuild after this one.

`app/api/routes_backend.py` survived the incomplete upstream push and is the
frozen consumer contract: `list_clusters` rows expose `.id`, `.label`,
`.member_count`, `.contradiction_flag`, `.contradiction_score`, and
`members_for_cluster` rows expose `.point_id`, `.paper_id`, `.text`, `.year`.

`replace_clusters` clears every table that hangs off `clusters` — the two
defined here and any a later phase adds (Stage 7's `cluster_trends`) — because
a rebuild invalidates all of them at once. See `_dependent_tables`.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    delete,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.clustering.schema import StatementCluster
from app.ingestion.corpus import Base


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Nullable by contract: the frontend falls back to `Cluster {id}` when a
    # cluster's members share no informative vocabulary (D-06-06).
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    contradiction_flag: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # 0.0 when nothing is flagged — Phase 8 multiplies this by `w5`.
    contradiction_score: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ClusterMember(Base):
    """One statement inside a cluster, denormalized enough to serve `/clusters`.

    `text` and `year` are copied rather than joined back to `extracted_facts`:
    the member's identity is a *Qdrant point*, the exact text that was embedded
    and clustered, and re-deriving it later would let the two drift.

    `paper_id` deliberately carries **no foreign key**. It arrives in a Qdrant
    payload, and the vector store can lag the corpus — a single stale point
    must not abort a whole corpus-wide rebuild with a constraint violation.
    """

    __tablename__ = "cluster_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    point_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    paper_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    text: Mapped[str] = mapped_column(String, nullable=False)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_type: Mapped[str] = mapped_column(String, nullable=False)


class ClusterContradiction(Base):
    """One flagged cross-paper disagreement, kept so the flag can be audited.

    `Cluster.contradiction_flag` is a boolean a gap score depends on; without
    the underlying pair a reviewer could not tell *which* two statements
    disagreed, and README §14's evidence-first rule would be words only.
    """

    __tablename__ = "cluster_contradictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    left_point_id: Mapped[str] = mapped_column(String, nullable=False)
    left_paper_id: Mapped[int] = mapped_column(Integer, nullable=False)
    right_point_id: Mapped[str] = mapped_column(String, nullable=False)
    right_paper_id: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)


def _dependent_tables() -> list[Table]:
    """Every table whose rows only mean something for the *current* cluster set.

    Discovered from the metadata rather than listed by hand. A cluster id is not
    stable across rebuilds, and on SQLite — the documented local-dev store — it
    is actively reused, because no foreign key is enforced and rowids restart
    after a full delete. Anything still pointing at a freed id therefore ends up
    silently attached to a different, unrelated theme: Stage 7 would serve one
    theme's time series under another's name.

    Reading it off the foreign keys means a later derived table (Phase 8's
    rankings) is covered by declaring its `ForeignKey("clusters.id")`, not by
    someone remembering to edit this function.
    """
    clusters = Cluster.__table__
    return [
        table
        for table in reversed(Base.metadata.sorted_tables)
        if table is not clusters
        and any(key.column.table is clusters for key in table.foreign_keys)
    ]


def replace_clusters(session: Session, clusters: list[StatementCluster]) -> list[int]:
    """Atomically replace every stored cluster within the caller's transaction.

    Flushes but never commits: `routes_backend` owns the transaction and
    commits after the call.

    Children are deleted explicitly rather than relying on `ondelete=CASCADE`,
    because SQLite does not enforce foreign keys by default — leaving orphaned
    members would silently inflate the next rebuild's `/clusters` payload.
    """
    from app.explanation.storage import archive_reports

    archive_reports(session)
    for table in _dependent_tables():
        session.execute(delete(table))
    session.execute(delete(Cluster))

    ids: list[int] = []
    for cluster in clusters:
        row = Cluster(
            label=cluster.label,
            member_count=cluster.member_count,
            contradiction_flag=cluster.contradiction_flag,
            contradiction_score=cluster.contradiction_score,
        )
        session.add(row)
        session.flush()
        session.add_all(
            ClusterMember(
                cluster_id=row.id,
                point_id=member.point_id,
                paper_id=member.paper_id,
                text=member.text,
                year=member.year,
                source_type=member.source_type,
            )
            for member in cluster.members
        )
        session.add_all(
            ClusterContradiction(
                cluster_id=row.id,
                left_point_id=pair.left_point_id,
                left_paper_id=pair.left_paper_id,
                right_point_id=pair.right_point_id,
                right_paper_id=pair.right_paper_id,
                score=pair.score,
            )
            for pair in cluster.contradictions
        )
        ids.append(row.id)

    session.flush()
    return ids


def list_clusters(session: Session) -> list[Cluster]:
    """Every cluster, largest first; ties broken by id so the order is stable."""
    return list(
        session.scalars(
            select(Cluster).order_by(Cluster.member_count.desc(), Cluster.id)
        )
    )


def members_for_cluster(session: Session, cluster_id: int) -> list[ClusterMember]:
    return list(
        session.scalars(
            select(ClusterMember)
            .where(ClusterMember.cluster_id == cluster_id)
            .order_by(ClusterMember.id)
        )
    )


def contradictions_for_cluster(
    session: Session, cluster_id: int
) -> list[ClusterContradiction]:
    return list(
        session.scalars(
            select(ClusterContradiction)
            .where(ClusterContradiction.cluster_id == cluster_id)
            .order_by(ClusterContradiction.score.desc(), ClusterContradiction.id)
        )
    )
