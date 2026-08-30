"""PostgreSQL/SQLAlchemy persistence for exact extracted evidence statements."""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    delete,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.extraction.schema import ExtractedStatement, FactType
from app.ingestion.corpus import Base


class ExtractedFact(Base):
    __tablename__ = "extracted_facts"
    __table_args__ = (
        UniqueConstraint(
            "paper_id",
            "fact_type",
            "section_index",
            "paragraph_index",
            "sentence_index",
            "text",
            name="uq_extracted_fact_evidence",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    paper_id: Mapped[int] = mapped_column(
        ForeignKey("papers.id"), nullable=False, index=True
    )
    fact_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    text: Mapped[str] = mapped_column(String, nullable=False)
    section_type: Mapped[str | None] = mapped_column(String, nullable=True)
    section_heading: Mapped[str | None] = mapped_column(String, nullable=True)
    section_index: Mapped[int] = mapped_column(Integer, nullable=False)
    paragraph_index: Mapped[int] = mapped_column(Integer, nullable=False)
    sentence_index: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    extraction_method: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


def _statement_key(statement: ExtractedStatement) -> tuple:
    return (
        statement.fact_type,
        statement.text,
        statement.section_type,
        statement.section_heading,
        statement.section_index,
        statement.paragraph_index,
        statement.sentence_index,
    )


def replace_facts(
    session: Session, paper_id: int, statements: list[ExtractedStatement]
) -> list[ExtractedFact]:
    """Atomically replace a paper's derived facts within the caller's transaction."""
    session.execute(delete(ExtractedFact).where(ExtractedFact.paper_id == paper_id))
    unique_statements = list(
        {_statement_key(item): item for item in statements}.values()
    )
    rows = [
        ExtractedFact(
            paper_id=paper_id,
            fact_type=statement.fact_type.value,
            text=statement.text,
            section_type=statement.section_type,
            section_heading=statement.section_heading,
            section_index=statement.section_index,
            paragraph_index=statement.paragraph_index,
            sentence_index=statement.sentence_index,
            confidence=statement.confidence,
            extraction_method=statement.extraction_method,
        )
        for statement in unique_statements
    ]
    session.add_all(rows)
    session.flush()
    return rows


def query_facts(
    session: Session, paper_id: int, fact_type: FactType | None = None
) -> list[ExtractedFact]:
    statement = select(ExtractedFact).where(ExtractedFact.paper_id == paper_id)
    if fact_type is not None:
        statement = statement.where(ExtractedFact.fact_type == fact_type.value)
    return list(session.scalars(statement.order_by(ExtractedFact.id)))
