"""Tests for backend/app/parsing/storage.py — ParsedDocument (D-28)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ingestion import corpus
from app.parsing.storage import (
    ParsedDocument,
    get_parsed_document,
    upsert_parsed_document,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    corpus.Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _insert_paper(session: Session) -> int:
    paper = corpus.insert_paper(
        session,
        {
            "title": "A Paper",
            "authors": ["A"],
            "year": 2023,
            "venue": "ACL",
            "doi": None,
            "abstract": "abstract",
            "source": "arxiv",
            "pdf_path": None,
            "has_full_text": True,
            "arxiv_id": "2301.00001",
            "acl_id": None,
        },
    )
    session.flush()
    return paper.id


def test_upsert_creates_and_get_retrieves(session: Session) -> None:
    paper_id = _insert_paper(session)
    sections = [
        {
            "heading": "Introduction",
            "type": "Introduction",
            "paragraphs": [{"text": "...", "sentences": ["..."]}],
        }
    ]

    upsert_parsed_document(
        session, paper_id=paper_id, sections=sections, parser_used="grobid"
    )

    doc = get_parsed_document(session, paper_id=paper_id)
    assert doc.sections == sections
    assert doc.parser_used == "grobid"


def test_upsert_updates_existing_row_in_place(session: Session) -> None:
    paper_id = _insert_paper(session)
    upsert_parsed_document(
        session, paper_id=paper_id, sections=[{"heading": "A"}], parser_used="grobid"
    )

    upsert_parsed_document(
        session, paper_id=paper_id, sections=[{"heading": "B"}], parser_used="fallback"
    )

    assert session.query(ParsedDocument).filter_by(paper_id=paper_id).count() == 1
    doc = get_parsed_document(session, paper_id=paper_id)
    assert doc.sections == [{"heading": "B"}]
    assert doc.parser_used == "fallback"


def test_get_returns_none_when_missing(session: Session) -> None:
    assert get_parsed_document(session, paper_id=999) is None


def test_paper_id_is_foreign_keyed_to_papers(session: Session) -> None:
    fks = ParsedDocument.__table__.c.paper_id.foreign_keys
    assert len(fks) > 0
    assert any(fk.column.table.name == "papers" for fk in fks)
