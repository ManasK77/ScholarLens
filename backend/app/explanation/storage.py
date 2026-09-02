from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, Text, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.ingestion.corpus import Base


class GapReport(Base):
    __tablename__ = "gap_reports"

    cluster_id: Mapped[int] = mapped_column(
        ForeignKey("semantic_clusters.id", ondelete="CASCADE"), primary_key=True
    )
    gap: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[list] = mapped_column(JSON, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    suggested_directions: Mapped[list] = mapped_column(JSON, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


def list_reports(session: Session) -> list[GapReport]:
    return list(session.scalars(select(GapReport).order_by(GapReport.cluster_id)))
