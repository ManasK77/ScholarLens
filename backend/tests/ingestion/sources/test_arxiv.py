"""Tests for the arXiv ingestion source (Plan 01-02).

Per D-13 (corrected): the installed `arxiv` v4.0.1 package has no
`download_pdf`/`download_source` helper on `Result` — PDFs must be fetched
via the shared `corpus.download_pdf` httpx helper against `Result.pdf_url`.
No test makes a real network call to arxiv.org (D-23).
"""

from datetime import UTC, datetime

from app.ingestion.sources import arxiv as arxiv_source


class FakeAuthor:
    def __init__(self, name: str):
        self.name = name


class FakeResult:
    """Stand-in for arxiv.Result — deliberately has no download_pdf/download_source."""

    def __init__(
        self,
        title,
        authors,
        published,
        summary,
        doi,
        short_id,
        pdf_url,
    ):
        self.title = title
        self.authors = authors
        self.published = published
        self.summary = summary
        self.doi = doi
        self._short_id = short_id
        self.pdf_url = pdf_url

    def get_short_id(self):
        return self._short_id


def make_result(**overrides):
    defaults = {
        "title": "Attention Is All You Need",
        "authors": [FakeAuthor("Ashish Vaswani"), FakeAuthor("Noam Shazeer")],
        "published": datetime(2017, 6, 12, tzinfo=UTC),
        "summary": "We propose the Transformer.",
        "doi": "10.1000/xyz",
        "short_id": "1706.03762",
        "pdf_url": "https://arxiv.org/pdf/1706.03762",
    }
    defaults.update(overrides)
    return FakeResult(
        title=defaults["title"],
        authors=defaults["authors"],
        published=defaults["published"],
        summary=defaults["summary"],
        doi=defaults["doi"],
        short_id=defaults["short_id"],
        pdf_url=defaults["pdf_url"],
    )


def test_search_topic_returns_corpus_shaped_dicts(monkeypatch):
    results = [
        make_result(),
        make_result(
            title="BERT",
            authors=[FakeAuthor("Jacob Devlin")],
            published=datetime(2018, 10, 11, tzinfo=UTC),
            summary="Bidirectional transformers.",
            doi="10.1000/bert",
            short_id="1810.04805",
            pdf_url="https://arxiv.org/pdf/1810.04805",
        ),
    ]

    def fake_results(self, search):
        yield from results

    monkeypatch.setattr(arxiv_source.arxiv.Client, "results", fake_results)

    out = arxiv_source.search_topic("transformer models")

    assert len(out) == 2
    first = out[0]
    assert first["title"] == "Attention Is All You Need"
    assert first["authors"] == ["Ashish Vaswani", "Noam Shazeer"]
    assert first["year"] == 2017
    assert first["venue"] is None
    assert first["doi"] == "10.1000/xyz"
    assert first["abstract"] == "We propose the Transformer."
    assert first["arxiv_id"] == "1706.03762"
    assert first["pdf_url"] == "https://arxiv.org/pdf/1706.03762"

    second = out[1]
    assert second["title"] == "BERT"
    assert second["authors"] == ["Jacob Devlin"]
    assert second["year"] == 2018


def test_search_topic_handles_missing_doi(monkeypatch):
    results = [make_result(doi=None)]

    def fake_results(self, search):
        yield from results

    monkeypatch.setattr(arxiv_source.arxiv.Client, "results", fake_results)

    out = arxiv_source.search_topic("transformer models")

    assert len(out) == 1
    assert out[0]["doi"] is None
    assert out[0]["arxiv_id"] == "1706.03762"


def test_fetch_pdf_delegates_to_corpus_download_pdf(monkeypatch):
    calls = {}

    def fake_download_pdf(url, dest_path, timeout=30.0):
        calls["url"] = url
        calls["dest_path"] = dest_path
        return True

    monkeypatch.setattr(arxiv_source.corpus, "download_pdf", fake_download_pdf)

    candidate = {
        "arxiv_id": "1706.03762",
        "pdf_url": "https://arxiv.org/pdf/1706.03762",
    }
    dest = arxiv_source.fetch_pdf(candidate, "/tmp/pdfs")

    assert calls["url"] == "https://arxiv.org/pdf/1706.03762"
    assert calls["dest_path"] == "/tmp/pdfs/1706.03762.pdf"
    assert dest == "/tmp/pdfs/1706.03762.pdf"


def test_fetch_pdf_returns_none_on_download_failure(monkeypatch):
    monkeypatch.setattr(
        arxiv_source.corpus, "download_pdf", lambda url, dest_path, timeout=30.0: False
    )

    candidate = {
        "arxiv_id": "1706.03762",
        "pdf_url": "https://arxiv.org/pdf/1706.03762",
    }
    dest = arxiv_source.fetch_pdf(candidate, "/tmp/pdfs")

    assert dest is None


def test_search_topic_never_calls_download_pdf_or_download_source_on_result(
    monkeypatch,
):
    """A bare fake object without download_pdf/download_source must not raise
    AttributeError from search_topic itself — proving the removed-API
    assumption isn't reintroduced.
    """

    class BareResult:
        def __init__(self):
            self.title = "No download helpers here"
            self.authors = [FakeAuthor("Someone")]
            self.published = datetime(2020, 1, 1, tzinfo=UTC)
            self.summary = "Abstract text."
            self.doi = None
            self.pdf_url = "https://arxiv.org/pdf/9999.99999"

        def get_short_id(self):
            return "9999.99999"

        def __getattr__(self, name):
            if name in ("download_pdf", "download_source"):
                raise AssertionError(
                    f"search_topic must never access Result.{name} (removed in arxiv v4.0.1)"
                )
            raise AttributeError(name)

    bare = BareResult()

    def fake_results(self, search):
        yield bare

    monkeypatch.setattr(arxiv_source.arxiv.Client, "results", fake_results)

    out = arxiv_source.search_topic("anything")

    assert len(out) == 1
    assert out[0]["arxiv_id"] == "9999.99999"
