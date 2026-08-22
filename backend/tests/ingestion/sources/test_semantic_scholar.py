"""Tests for the Semantic Scholar ingestion source (Plan 01-03).

Metadata-only per README/D-14 — this source never downloads a PDF itself;
it only stores the `openAccessPdf.url` value for later enrichment. No test
makes a real network call to api.semanticscholar.org (D-23).
"""

import inspect

import httpx

from app.ingestion.sources import semantic_scholar


class FakeResponse:
    """Stand-in for httpx.Response — only .json() and .raise_for_status() are used."""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


FIXTURE_BODY = {
    "total": 2,
    "offset": 0,
    "next": 2,
    "data": [
        {
            "title": "Graph Neural Networks: A Review",
            "authors": [
                {"authorId": "1", "name": "Alice Author"},
                {"authorId": "2", "name": "Bob Author"},
            ],
            "year": 2020,
            "venue": "JMLR",
            "externalIds": {"DOI": "10.1000/gnn-review"},
            "abstract": "A survey of GNNs.",
            "openAccessPdf": {"url": "https://example.org/gnn-review.pdf"},
        },
        {
            "title": "Message Passing Neural Networks",
            "authors": [{"authorId": "3", "name": "Carol Author"}],
            "year": 2019,
            "venue": None,
            "externalIds": None,
            "abstract": None,
            "openAccessPdf": None,
        },
    ],
}


def test_search_topic_returns_corpus_shaped_dicts(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None, transport=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return FakeResponse(FIXTURE_BODY)

    monkeypatch.setattr(httpx, "get", fake_get)

    out = semantic_scholar.search_topic("graph neural networks", api_key=None)

    assert len(out) == 2
    first = out[0]
    assert first["title"] == "Graph Neural Networks: A Review"
    assert first["authors"] == ["Alice Author", "Bob Author"]
    assert first["year"] == 2020
    assert first["venue"] == "JMLR"
    assert first["doi"] == "10.1000/gnn-review"
    assert first["abstract"] == "A survey of GNNs."
    assert first["openaccess_pdf_url"] == "https://example.org/gnn-review.pdf"

    assert captured["url"] == "https://api.semanticscholar.org/graph/v1/paper/search"


def test_search_topic_handles_missing_openaccess_pdf(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None, transport=None):
        return FakeResponse(FIXTURE_BODY)

    monkeypatch.setattr(httpx, "get", fake_get)

    out = semantic_scholar.search_topic("graph neural networks", api_key=None)

    second = out[1]
    assert second["openaccess_pdf_url"] is None
    assert second["doi"] is None
    assert second["venue"] is None
    assert second["abstract"] is None
    assert second["authors"] == ["Carol Author"]


def test_search_topic_sends_api_key_header_when_provided(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None, transport=None):
        captured["headers"] = headers
        return FakeResponse(FIXTURE_BODY)

    monkeypatch.setattr(httpx, "get", fake_get)

    semantic_scholar.search_topic("graph neural networks", api_key="test-key-123")

    assert captured["headers"] == {"x-api-key": "test-key-123"}


def test_search_topic_sends_no_api_key_header_when_absent(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None, transport=None):
        captured["headers"] = headers
        return FakeResponse(FIXTURE_BODY)

    monkeypatch.setattr(httpx, "get", fake_get)

    semantic_scholar.search_topic("graph neural networks", api_key=None)

    assert captured["headers"] == {}
    assert "x-api-key" not in captured["headers"]


def test_source_is_metadata_only_never_calls_download_pdf(monkeypatch):
    source_text = inspect.getsource(semantic_scholar)
    assert "download_pdf" not in source_text

    def fake_get(url, params=None, headers=None, timeout=None, transport=None):
        return FakeResponse(FIXTURE_BODY)

    monkeypatch.setattr(httpx, "get", fake_get)

    out = semantic_scholar.search_topic("graph neural networks", api_key=None)

    for paper in out:
        assert "pdf_path" not in paper
        assert "pdf_bytes" not in paper
