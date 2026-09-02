"""PostgreSQL persistence for semantic clusters and contradiction evidence."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.ingestion.corpus import Base


class SemanticCluster(Base):
    __tablename__ = "semantic_clusters"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    contradiction_flag: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    contradiction_score: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ClusterMember(Base):
    __tablename__ = "cluster_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        ForeignKey("semantic_clusters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    point_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    paper_id: Mapped[int] = mapped_column(
        ForeignKey("papers.id"), nullable=False, index=True
    )
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ContradictionEvidence(Base):
    __tablename__ = "contradiction_evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        ForeignKey("semantic_clusters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    left_point_id: Mapped[str] = mapped_column(String, nullable=False)
    right_point_id: Mapped[str] = mapped_column(String, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)


def list_clusters(session: Session) -> list[SemanticCluster]:
    return list(session.scalars(select(SemanticCluster).order_by(SemanticCluster.id)))


def members_for_cluster(session: Session, cluster_id: int) -> list[ClusterMember]:
    return list(
        session.scalars(
            select(ClusterMember)
            .where(ClusterMember.cluster_id == cluster_id)
            .order_by(ClusterMember.id)
        )
    )
