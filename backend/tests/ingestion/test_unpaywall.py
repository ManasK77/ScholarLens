"""Tests for backend/app/ingestion/unpaywall.py — INGEST-06 OA enrichment.

D-17/D-23: no test makes a real network call to api.unpaywall.org. Pattern 3:
enrichment is a targeted UPDATE (corpus.enrich_paper) against an existing row,
never an INSERT.
"""

import httpx
import pytest

from app.ingestion import corpus
from app.ingestion.unpaywall import enrich, resolve_oa_pdf


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise AssertionError(".json() should not be called on a non-200 response")
        return self._payload


def _make_fake_client(response: _FakeResponse):
    # httpx.get() (module-level shortcut) doesn't accept a `transport` kwarg —
    # only httpx.Client() does — so resolve_oa_pdf uses a context-managed
    # httpx.Client(transport=...); tests fake that, not httpx.get directly.
    class FakeClient:
        def __init__(self, transport=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, timeout=None):
            return response

    return FakeClient


def test_resolve_oa_pdf_returns_url_when_oa_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse(
        200,
        {
            "is_oa": True,
            "best_oa_location": {"url_for_pdf": "https://example.org/paper.pdf"},
        },
    )
    monkeypatch.setattr(httpx, "Client", _make_fake_client(response))

    result = resolve_oa_pdf("10.1/xyz", "test@example.org")

    assert result == "https://example.org/paper.pdf"


def test_resolve_oa_pdf_returns_none_when_no_oa_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse(200, {"is_oa": False, "best_oa_location": None})
    monkeypatch.setattr(httpx, "Client", _make_fake_client(response))

    result = resolve_oa_pdf("10.1/valid-no-oa", "test@example.org")

    assert result is None


def test_resolve_oa_pdf_short_circuits_on_404_before_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # payload=None makes .json() raise if ever called — proves the status_code
    # guard runs before any JSON parsing (Pitfall 2).
    response = _FakeResponse(404, None)
    monkeypatch.setattr(httpx, "Client", _make_fake_client(response))

    result = resolve_oa_pdf("10.1/not-a-real-doi", "test@example.org")

    assert result is None


def test_resolve_oa_pdf_falls_back_to_url_when_no_url_for_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse(
        200,
        {
            "is_oa": True,
            "best_oa_location": {
                "url_for_pdf": None,
                "url": "https://example.org/landing",
            },
        },
    )
    monkeypatch.setattr(httpx, "Client", _make_fake_client(response))

    result = resolve_oa_pdf("10.1/xyz", "test@example.org")

    assert result == "https://example.org/landing"


def test_enrich_calls_enrich_paper_never_insert_paper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRow:
        id = 42
        doi = "10.1/xyz"

    enrich_paper_calls = []
    insert_paper_calls = []

    monkeypatch.setattr(corpus, "rows_missing_fulltext", lambda session: [FakeRow()])
    monkeypatch.setattr(
        "app.ingestion.unpaywall.resolve_oa_pdf",
        lambda doi, email: "https://example.org/paper.pdf",
    )
    monkeypatch.setattr(corpus, "download_pdf", lambda url, dest_path, **kw: True)
    monkeypatch.setattr(
        corpus,
        "enrich_paper",
        lambda session, paper_id, updates: enrich_paper_calls.append(
            (paper_id, updates)
        ),
    )
    monkeypatch.setattr(
        corpus,
        "insert_paper",
        lambda session, data: insert_paper_calls.append(data),
    )

    result = enrich(session=object(), email="test@example.org", dest_dir="/tmp/pdfs")

    assert enrich_paper_calls == [
        (42, {"has_full_text": True, "pdf_path": "/tmp/pdfs/42.pdf"})
    ]
    assert insert_paper_calls == []
    assert result == [42]


def test_enrich_skips_rows_with_no_oa_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRow:
        id = 7
        doi = "10.1/no-oa"

    enrich_paper_calls = []

    monkeypatch.setattr(corpus, "rows_missing_fulltext", lambda session: [FakeRow()])
    monkeypatch.setattr(
        "app.ingestion.unpaywall.resolve_oa_pdf", lambda doi, email: None
    )
    monkeypatch.setattr(
        corpus,
        "enrich_paper",
        lambda session, paper_id, updates: enrich_paper_calls.append(
            (paper_id, updates)
        ),
    )

    result = enrich(session=object(), email="test@example.org", dest_dir="/tmp/pdfs")

    assert enrich_paper_calls == []
    assert result == []
