"""Normalized corpus writer (D-10, D-11) — the shared contract every ingestion
source and Unpaywall enrichment writes through.

Stage 2 onward reads from this schema without ever branching on `source`
(CLAUDE.md hard constraint 5). No migration framework is used (D-11) —
`Base.metadata.create_all()` is run once at FastAPI startup (see app.main).
"""

from collections.abc import Generator
from datetime import datetime
from functools import lru_cache

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
    # Semantic Scholar's `citationCount` (README §7 — the only source that
    # reports one). Feeds Phase 8's `citation_impact` signal and `GET /papers`.
    # Nullable rather than defaulted to 0: an arXiv-only or manually uploaded
    # record has no count, and 0 would assert that nobody cited it.
    citation_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Provenance kept for full-text acquisition (D-FULLTEXT-01, 2026-10-12):
    # Semantic Scholar's `openAccessPdf` URL, and the record's page at its
    # source (arXiv abstract, Semantic Scholar paper, ACL Anthology entry).
    # Both were dropped on the way into this table before.
    openaccess_pdf_url: Mapped[str | None] = mapped_column(String, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


@lru_cache(maxsize=1)
def get_engine():
    """A single cached Engine (and its connection pool) for the process lifetime.

    Building a fresh Engine per request would open a new physical connection
    pool every time, churning connections and risking exhausting Postgres's
    max_connections under any sustained traffic.
    """
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
    """Store the PDF at `url`; False on any failure, including a non-PDF body.

    A 200 is not proof of a PDF: an Unpaywall `best_oa_location.url` is often a
    publisher landing page, and storing that as `has_full_text` hands Stage 2 an
    HTML "document" and stops Unpaywall from ever trying the paper again.

    Delegates to `app.ingestion.fulltext.safe_download` (D-FULLTEXT-01): only
    public HTTP(S) hosts, redirects re-checked, a size limit, and a body
    written beside `dest_path` and moved into place only once it is known to
    be a PDF -- /collect fetches before dedup, so a re-collected paper
    downloads onto the file its stored row already points at, and a failed
    second fetch must not destroy the first.
    """
    from app.ingestion.fulltext import safe_download

    return safe_download(url, dest_path, timeout=timeout).ok
