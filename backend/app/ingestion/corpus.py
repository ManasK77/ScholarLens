"""Normalized corpus writer (D-10, D-11) — the shared contract every ingestion
source and Unpaywall enrichment writes through.

Stage 2 onward reads from this schema without ever branching on `source`
(CLAUDE.md hard constraint 5). No migration framework is used (D-11) —
`Base.metadata.create_all()` is run once at FastAPI startup (see app.main).
"""

from collections.abc import Generator
from datetime import datetime
from pathlib import Path

import httpx
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    String,
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    authors: Mapped[list | None] = mapped_column(JSON, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    venue: Mapped[str | None] = mapped_column(String, nullable=True)
    doi: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    abstract: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    pdf_path: Mapped[str | None] = mapped_column(String, nullable=True)
    has_full_text: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    arxiv_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    acl_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


def get_engine():
    return create_engine(
        get_settings().postgres_url, connect_args={"connect_timeout": 5}
    )


def get_session() -> Generator[Session, None, None]:
    session_factory = sessionmaker(bind=get_engine())
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def insert_paper(session: Session, data: dict) -> Paper:
    paper = Paper(**data)
    session.add(paper)
    session.flush()
    return paper


def enrich_paper(session: Session, paper_id: int, updates: dict) -> Paper:
    paper = session.get(Paper, paper_id)
    for key, value in updates.items():
        setattr(paper, key, value)
    session.flush()
    return paper


def rows_missing_fulltext(session: Session) -> list[Paper]:
    stmt = select(Paper).where(Paper.doi.is_not(None), Paper.has_full_text.is_(False))
    return list(session.scalars(stmt))


def download_pdf(url: str, dest_path: str, timeout: float = 30.0) -> bool:
    try:
        with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
            if resp.status_code != 200:
                return False
            Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "wb") as f:
                f.writelines(resp.iter_bytes())
        return True
    except httpx.HTTPError:
        return False
