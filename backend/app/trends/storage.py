from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.ingestion.corpus import Base


class ClusterTrend(Base):
    __tablename__ = "cluster_trends"

    cluster_id: Mapped[int] = mapped_column(
        ForeignKey("semantic_clusters.id", ondelete="CASCADE"), primary_key=True
    )
    yearly_counts: Mapped[dict] = mapped_column(JSON, nullable=False)
    slope: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


def list_trends(session: Session) -> list[ClusterTrend]:
    return list(session.scalars(select(ClusterTrend).order_by(ClusterTrend.cluster_id)))
