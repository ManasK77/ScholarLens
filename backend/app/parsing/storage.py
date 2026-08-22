"""ParsedDocument storage (D-28) — one row per paper, sections stored as JSON.

Shares app.ingestion.corpus's Base declarative metadata so Base.metadata.
create_all() creates both `papers` and `parsed_documents` together.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.ingestion.corpus import Base


class ParsedDocument(Base):
    __tablename__ = "parsed_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    paper_id: Mapped[int] = mapped_column(
        ForeignKey("papers.id"), nullable=False, unique=True, index=True
    )
    sections: Mapped[list] = mapped_column(JSON, nullable=False)
    parser_used: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


def upsert_parsed_document(
    session: Session, paper_id: int, sections: list, parser_used: str
) -> ParsedDocument:
    existing = session.scalar(
        select(ParsedDocument).where(ParsedDocument.paper_id == paper_id)
    )
    if existing:
        existing.sections = sections
        existing.parser_used = parser_used
        session.flush()
        return existing

    doc = ParsedDocument(paper_id=paper_id, sections=sections, parser_used=parser_used)
    session.add(doc)
    session.flush()
    return doc


def get_parsed_document(session: Session, paper_id: int) -> ParsedDocument | None:
    return session.scalar(
        select(ParsedDocument).where(ParsedDocument.paper_id == paper_id)
    )
