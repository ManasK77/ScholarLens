"""Tests for backend/app/ingestion/dedup.py — the D-19 priority-chain matcher."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ingestion.corpus import Base, insert_paper
from app.ingestion.dedup import find_existing, merge, normalize_title


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _paper_data(**overrides):
    data = {
        "title": "Deep Learning for NLP",
        "authors": ["Alice"],
        "year": 2023,
        "venue": "ACL",
        "doi": None,
        "abstract": "An abstract.",
        "source": "arxiv",
        "pdf_path": None,
        "has_full_text": False,
        "arxiv_id": None,
        "acl_id": None,
    }
    data.update(overrides)
    return data


def test_doi_match_wins_first(session: Session) -> None:
    existing = insert_paper(session, _paper_data(doi="10.1/x", arxiv_id="2301.00001"))
    session.flush()

    candidate = _paper_data(doi="10.1/x", arxiv_id="9999.99999")
    found = find_existing(session, candidate)

    assert found.id == existing.id


def test_arxiv_id_match_when_no_doi(session: Session) -> None:
    existing = insert_paper(session, _paper_data(doi=None, arxiv_id="2301.00001"))
    session.flush()

    candidate = _paper_data(doi=None, arxiv_id="2301.00001")
    found = find_existing(session, candidate)

    assert found.id == existing.id


def test_acl_id_match_when_no_doi_or_arxiv(session: Session) -> None:
    existing = insert_paper(
        session, _paper_data(doi=None, arxiv_id=None, acl_id="2023.acl-long.310")
    )
    session.flush()

    candidate = _paper_data(doi=None, arxiv_id=None, acl_id="2023.acl-long.310")
    found = find_existing(session, candidate)

    assert found.id == existing.id


def test_normalized_title_year_fallback(session: Session) -> None:
    existing = insert_paper(
        session,
        _paper_data(
            doi=None,
            arxiv_id=None,
            acl_id=None,
            title="Deep Learning For NLP",
            year=2023,
        ),
    )
    session.flush()

    candidate = _paper_data(
        doi=None, arxiv_id=None, acl_id=None, title="deep learning for nlp!!", year=2023
    )
    found = find_existing(session, candidate)

    assert found.id == existing.id


def test_no_match_returns_none(session: Session) -> None:
    insert_paper(session, _paper_data(doi="10.1/a"))
    session.flush()

    candidate = _paper_data(
        doi="10.1/completely-different", title="Something Else", year=1999
    )
    found = find_existing(session, candidate)

    assert found is None


def test_normalize_title() -> None:
    assert normalize_title("Deep Learning For NLP") == normalize_title(
        "deep learning for nlp!!"
    )


def test_merge_prefers_present_over_none() -> None:
    existing = type(
        "FakePaper",
        (),
        {"title": None, "doi": "10.1/x", "has_full_text": False, "pdf_path": None},
    )()
    candidate = {
        "title": "New Title",
        "doi": None,
        "has_full_text": False,
        "pdf_path": None,
    }

    result = merge(existing, candidate)

    assert result["title"] == "New Title"
    assert result["doi"] == "10.1/x"


def test_merge_never_overwrites_present_with_none() -> None:
    existing = type(
        "FakePaper",
        (),
        {
            "title": "Existing Title",
            "has_full_text": False,
            "pdf_path": None,
            "doi": None,
        },
    )()
    candidate = {"title": None, "has_full_text": False, "pdf_path": None, "doi": None}

    result = merge(existing, candidate)

    assert result["title"] == "Existing Title"


def test_merge_prefers_full_text_true() -> None:
    existing = type(
        "FakePaper",
        (),
        {"title": None, "doi": None, "has_full_text": True, "pdf_path": "/data/1.pdf"},
    )()
    candidate = {"title": None, "doi": None, "has_full_text": False, "pdf_path": None}

    result = merge(existing, candidate)

    assert result["has_full_text"] is True
    assert result["pdf_path"] == "/data/1.pdf"
