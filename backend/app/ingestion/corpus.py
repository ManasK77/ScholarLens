"""Normalized corpus writer (D-10, D-11) — the shared contract every ingestion
source and Unpaywall enrichment writes through.

Stage 2 onward reads from this schema without ever branching on `source`
(CLAUDE.md hard constraint 5). No migration framework is used (D-11) —
`Base.metadata.create_all()` is run once at FastAPI startup (see app.main).
"""

from collections.abc import Generator
from datetime import datetime
from functools import lru_cache
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
    # Semantic Scholar's `citationCount` (README §7 — the only source that
    # reports one). Feeds Phase 8's `citation_impact` signal and `GET /papers`.
    # Nullable rather than defaulted to 0: an arXiv-only or manually uploaded
    # record has no count, and 0 would assert that nobody cited it.
    citation_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
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


# PDF readers accept the `%PDF-` header anywhere in a file's first 1024 bytes.
_PDF_MAGIC = b"%PDF-"
_PDF_HEADER_WINDOW = 1024


def download_pdf(url: str, dest_path: str, timeout: float = 30.0) -> bool:
    """Store the PDF at `url`; False on any failure, including a non-PDF body.

    A 200 is not proof of a PDF: an Unpaywall `best_oa_location.url` is often a
    publisher landing page, and storing that as `has_full_text` hands Stage 2 an
    HTML "document" and stops Unpaywall from ever trying the paper again.

    The body is written beside `dest_path` and moved into place only once it
    is known to be a PDF: /collect fetches before dedup, so a re-collected
    paper downloads onto the file its stored row already points at, and a
    failed second fetch must not destroy the first.
    """
    path = Path(dest_path)
    partial = path.with_name(f"{path.name}.part")
    try:
        with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
            if resp.status_code != 200:
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(partial, "wb") as f:
                f.writelines(resp.iter_bytes())
        with open(partial, "rb") as f:
            is_pdf = _PDF_MAGIC in f.read(_PDF_HEADER_WINDOW)
        if is_pdf:
            partial.replace(path)
        return is_pdf
    except httpx.HTTPError:
        return False
    finally:
        partial.unlink(missing_ok=True)
