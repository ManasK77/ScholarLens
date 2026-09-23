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


# A 200 answer is not proof of a PDF. Unpaywall's `best_oa_location.url` is
# often a publisher landing page; the stored corpus once held a Scientific
# Reports "JavaScript is disabled in your browser" HTML page as `14.pdf`,
# flagged has_full_text=True, which Stage 2's fallback parser then turned into
# a one-sentence "document" and which Unpaywall never retried.


def _streaming(chunks, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    class FakeResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self):
            yield from chunks

    monkeypatch.setattr(
        httpx,
        "stream",
        lambda method, url, timeout=30.0, follow_redirects=True: FakeResponse(),
    )


def test_download_pdf_rejects_an_html_page_served_with_200(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _streaming(
        [b"<!DOCTYPE html><html><body>JavaScript is disabled in your browser."],
        monkeypatch,
    )

    dest = tmp_path / "out.pdf"
    result = download_pdf("https://www.nature.com/articles/x", str(dest))

    assert result is False
    assert not dest.exists()


def test_download_pdf_rejects_an_empty_200_body(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _streaming([], monkeypatch)

    dest = tmp_path / "out.pdf"

    assert download_pdf("https://example.org/paper.pdf", str(dest)) is False
    assert not dest.exists()


def test_download_pdf_accepts_a_pdf_header_split_across_chunks(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _streaming([b"%P", b"DF-1.7\n", b"body"], monkeypatch)

    dest = tmp_path / "out.pdf"

    assert download_pdf("https://example.org/paper.pdf", str(dest)) is True
    assert dest.read_bytes() == b"%PDF-1.7\nbody"


def test_download_pdf_accepts_bytes_before_the_pdf_header(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readers accept `%PDF-` anywhere in the first 1024 bytes; so does this."""
    _streaming([b"\xef\xbb\xbf\r\n%PDF-1.4 content"], monkeypatch)

    dest = tmp_path / "out.pdf"

    assert download_pdf("https://example.org/paper.pdf", str(dest)) is True


def test_download_pdf_non_pdf_leaves_no_file_behind(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejecting a body must not leave a half-written or HTML file behind."""
    _streaming([b"<html>landing page</html>"], monkeypatch)

    dest = tmp_path / "out.pdf"
    result = download_pdf("https://example.org/paper", str(dest))

    assert result is False
    assert list(tmp_path.iterdir()) == []


def test_download_pdf_rejected_body_keeps_an_existing_good_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/collect fetches a candidate's PDF before dedup, so re-collecting an
    ACL paper downloads onto the path its stored PDF already occupies. A bad
    second answer must leave the first, valid file in place."""
    dest = tmp_path / "2025.acl-long.1.pdf"
    dest.write_bytes(b"%PDF-1.7 the good copy")
    _streaming([b"<html>maintenance page</html>"], monkeypatch)

    assert (
        download_pdf("https://aclanthology.org/2025.acl-long.1.pdf", str(dest)) is False
    )
    assert dest.read_bytes() == b"%PDF-1.7 the good copy"
    assert list(tmp_path.iterdir()) == [dest]


def test_download_pdf_interrupted_stream_keeps_an_existing_good_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    def chunks():
        yield b"%PDF-1.7 partial"
        raise httpx.ReadError("connection reset")

    dest = tmp_path / "paper.pdf"
    dest.write_bytes(b"%PDF-1.7 the good copy")
    _streaming(chunks(), monkeypatch)

    assert download_pdf("https://example.org/paper.pdf", str(dest)) is False
    assert dest.read_bytes() == b"%PDF-1.7 the good copy"
    assert list(tmp_path.iterdir()) == [dest]
