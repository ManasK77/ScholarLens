"""Tests for backend/app/ingestion/corpus.py — the D-10 shared corpus contract.

Uses an in-memory SQLite engine so tests never touch real Postgres, matching
D-23's no-real-network/service-call rule extended to DB isolation.
"""

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ingestion.corpus import (
    Base,
    Paper,
    download_pdf,
    enrich_paper,
    get_session,
    insert_paper,
    rows_missing_fulltext,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _paper_data(**overrides):
    data = {
        "title": "Deep Learning for NLP",
        "authors": ["Alice", "Bob"],
        "year": 2023,
        "venue": "ACL",
        "doi": "10.1/xyz",
        "abstract": "An abstract.",
        "source": "arxiv",
        "pdf_path": None,
        "has_full_text": False,
        "arxiv_id": "2301.00001",
        "acl_id": None,
    }
    data.update(overrides)
    return data


def test_insert_paper_roundtrips_all_fields(session: Session) -> None:
    paper = insert_paper(session, _paper_data())

    fetched = session.get(Paper, paper.id)
    assert fetched.title == "Deep Learning for NLP"
    assert fetched.authors == ["Alice", "Bob"]
    assert fetched.year == 2023
    assert fetched.venue == "ACL"
    assert fetched.doi == "10.1/xyz"
    assert fetched.abstract == "An abstract."
    assert fetched.source == "arxiv"
    assert fetched.has_full_text is False
    assert fetched.arxiv_id == "2301.00001"
    assert fetched.acl_id is None
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


def test_insert_paper_requires_source(session: Session) -> None:
    data = _paper_data()
    del data["source"]
    with pytest.raises(IntegrityError):
        insert_paper(session, data)
        session.flush()


def test_enrich_paper_updates_only_given_fields(session: Session) -> None:
    paper = insert_paper(session, _paper_data())
    original_id = paper.id

    updated = enrich_paper(
        session, paper.id, {"has_full_text": True, "pdf_path": "/data/1.pdf"}
    )

    assert updated.id == original_id
    assert updated.has_full_text is True
    assert updated.pdf_path == "/data/1.pdf"
    assert updated.title == "Deep Learning for NLP"
    assert session.query(Paper).count() == 1


def test_rows_missing_fulltext_filters_correctly(session: Session) -> None:
    missing = insert_paper(session, _paper_data(doi="10.1/a", has_full_text=False))
    insert_paper(session, _paper_data(doi=None, has_full_text=False))
    insert_paper(session, _paper_data(doi="10.1/b", has_full_text=True))
    session.flush()

    results = rows_missing_fulltext(session)

    assert [p.id for p in results] == [missing.id]


def test_download_pdf_returns_true_and_writes_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self):
            yield b"%PDF-1.4 fake content"

    def fake_stream(method, url, timeout=30.0, follow_redirects=True):
        return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "stream", fake_stream)

    dest = tmp_path / "out.pdf"
    result = download_pdf("https://example.org/paper.pdf", str(dest))

    assert result is True
    assert dest.read_bytes() == b"%PDF-1.4 fake content"


def test_download_pdf_returns_false_on_non_200(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeResponse:
        status_code = 404

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self):
            yield b""

    def fake_stream(method, url, timeout=30.0, follow_redirects=True):
        return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "stream", fake_stream)

    dest = tmp_path / "out.pdf"
    result = download_pdf("https://example.org/missing.pdf", str(dest))

    assert result is False
    assert not dest.exists()


def test_download_pdf_returns_false_on_http_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    def fake_stream(method, url, timeout=30.0, follow_redirects=True):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "stream", fake_stream)

    dest = tmp_path / "out.pdf"
    result = download_pdf("https://example.org/paper.pdf", str(dest))

    assert result is False
    assert not dest.exists()


def test_get_session_yields_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.ingestion.corpus as corpus_module

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(corpus_module, "get_engine", lambda: engine)

    gen = get_session()
    session = next(gen)
    assert isinstance(session, Session)
    session.execute(Paper.__table__.select())

    with pytest.raises(StopIteration):
        next(gen)


def test_papers_carry_a_nullable_citation_count():
    """Read by `GET /papers` and by Phase 8's `citation_impact` signal.

    Nullable because only Semantic Scholar reports it: an arXiv-only or
    manually uploaded record has no count, and `0` would be a false claim that
    nobody cited it.
    """
    column = Paper.__table__.columns["citation_count"]

    assert column.nullable is True
    assert isinstance(column.type, Integer)
