from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.ingestion.corpus import Base


class GapRanking(Base):
    __tablename__ = "gap_rankings"

    cluster_id: Mapped[int] = mapped_column(
        ForeignKey("semantic_clusters.id", ondelete="CASCADE"), primary_key=True
    )
    score: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    mention_frequency: Mapped[float] = mapped_column(Float, nullable=False)
    trend_slope: Mapped[float] = mapped_column(Float, nullable=False)
    solution_density: Mapped[float] = mapped_column(Float, nullable=False)
    citation_impact: Mapped[float] = mapped_column(Float, nullable=False)
    contradiction_flag: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


def list_rankings(session: Session, limit: int | None = None) -> list[GapRanking]:
    statement = select(GapRanking).order_by(GapRanking.score.desc())
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))
