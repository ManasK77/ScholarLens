"""Semantic Scholar publication-type validation (D-S2-TYPES-01).

Semantic Scholar tags each record with a structured `publicationTypes` list.
Records tagged as clearly non-research documents (Editorial, News,
LettersAndComments, CaseReport) are dropped before they become candidates, so
they never reach `_process_capped` and never occupy a
`semantic_scholar_max_results` slot. Nothing here reads a title, abstract or
PDF: the decision is made on the structured metadata field alone.

No test makes a real network call and no test sleeps.
"""

from typing import ClassVar

import httpx
import pytest

from app.ingestion.sources import semantic_scholar
from tests.ingestion.fake_clock import FakeClock


@pytest.fixture(autouse=True)
def _no_real_waiting(monkeypatch):
    monkeypatch.setattr(semantic_scholar, "LIMITER", FakeClock().limiter())


class _Response:
    status_code = 200
    headers: ClassVar[dict] = {}

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _record(title, publication_types=..., **extra):
    record = {
        "title": title,
        "authors": [{"authorId": "1", "name": "Author"}],
        "year": 2025,
        "venue": "Venue",
        "externalIds": {},
        "abstract": "Abstract.",
        "openAccessPdf": None,
        "citationCount": 1,
    }
    if publication_types is not ...:
        record["publicationTypes"] = publication_types
    record.update(extra)
    return record


def _serve(monkeypatch, records):
    calls = []

    class Client:
        def __init__(self, transport=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, headers=None, timeout=None):
            calls.append(params)
            return _Response({"data": records})

    monkeypatch.setattr(httpx, "Client", Client)
    return calls


def _titles(results):
    return [r["title"] for r in results]


def test_publication_types_are_requested(monkeypatch):
    calls = _serve(monkeypatch, [])

    semantic_scholar.search_topic("prompt injection")

    assert "publicationTypes" in calls[0]["fields"].split(",")


@pytest.mark.parametrize(
    "rejected", ["Editorial", "News", "LettersAndComments", "CaseReport"]
)
def test_each_non_research_type_is_rejected(monkeypatch, rejected):
    _serve(
        monkeypatch,
        [_record("Kept", ["JournalArticle"]), _record("Dropped", [rejected])],
    )

    out = semantic_scholar.search_topic("prompt injection")

    assert _titles(out) == ["Kept"]


@pytest.mark.parametrize(
    "accepted", ["Review", "Study", "Conference", "JournalArticle"]
)
def test_research_types_are_accepted(monkeypatch, accepted):
    _serve(monkeypatch, [_record("Paper", [accepted])])

    out = semantic_scholar.search_topic("prompt injection")

    assert _titles(out) == ["Paper"]


def test_missing_publication_types_is_accepted(monkeypatch):
    """Absence of a type is not evidence of a non-research document."""
    _serve(monkeypatch, [_record("No key")])

    assert _titles(semantic_scholar.search_topic("prompt injection")) == ["No key"]


@pytest.mark.parametrize("empty", [None, []])
def test_null_or_empty_publication_types_is_accepted(monkeypatch, empty):
    _serve(monkeypatch, [_record("Untyped", empty)])

    assert _titles(semantic_scholar.search_topic("prompt injection")) == ["Untyped"]


def test_a_venue_type_does_not_rescue_a_non_research_type(monkeypatch):
    """`JournalArticle`/`Conference` say where it appeared, not what it is: an
    editorial printed in a journal is still an editorial."""
    _serve(
        monkeypatch,
        [
            _record("Journal editorial", ["JournalArticle", "Editorial"]),
            _record("Conference news", ["News", "Conference"]),
        ],
    )

    assert semantic_scholar.search_topic("prompt injection") == []


@pytest.mark.parametrize("research", ["Study", "Review"])
def test_an_explicit_research_type_keeps_a_mixed_record(monkeypatch, research):
    """Only *clearly* non-research records are dropped: one that Semantic
    Scholar also tags as a study or a review is kept."""
    _serve(monkeypatch, [_record("Research letter", ["LettersAndComments", research])])

    assert _titles(semantic_scholar.search_topic("prompt injection")) == [
        "Research letter"
    ]


def test_publication_types_is_not_a_corpus_column(monkeypatch):
    """The candidate dict keeps the existing corpus-shaped contract."""
    _serve(monkeypatch, [_record("Paper", ["Study"])])

    (out,) = semantic_scholar.search_topic("prompt injection")

    assert "publication_types" not in out
    assert "publicationTypes" not in out


# ---------------------------------------------------------------------------
# Source-cap interaction: rejected records never take a slot
# ---------------------------------------------------------------------------


def test_a_larger_page_is_requested_than_is_kept(monkeypatch):
    calls = _serve(monkeypatch, [])

    semantic_scholar.search_topic("prompt injection", limit=10, request_limit=20)

    assert calls[0]["limit"] == 20


def test_the_request_is_never_smaller_than_what_is_kept(monkeypatch):
    calls = _serve(monkeypatch, [])

    semantic_scholar.search_topic("prompt injection", limit=10, request_limit=5)

    assert calls[0]["limit"] == 10


def test_the_request_never_exceeds_the_api_maximum(monkeypatch):
    calls = _serve(monkeypatch, [])

    semantic_scholar.search_topic("prompt injection", limit=10, request_limit=500)

    assert calls[0]["limit"] == 100


def test_rejected_records_do_not_consume_the_cap(monkeypatch):
    """Ten research papers are still returned when non-research records are
    interleaved among the first ten results."""
    records = []
    for i in range(14):
        records.append(_record(f"Paper {i}", ["JournalArticle"]))
        if i < 4:
            records.append(_record(f"Editorial {i}", ["Editorial"]))
    _serve(monkeypatch, records)

    out = semantic_scholar.search_topic("prompt injection", limit=10, request_limit=20)

    assert _titles(out) == [f"Paper {i}" for i in range(10)]


def test_results_keep_semantic_scholar_relevance_order(monkeypatch):
    _serve(
        monkeypatch,
        [
            _record("First", ["Study"]),
            _record("Dropped", ["News"]),
            _record("Second", None),
            _record("Third", ["Review"]),
        ],
    )

    out = semantic_scholar.search_topic("prompt injection", limit=2)

    assert _titles(out) == ["First", "Second"]


def test_a_page_of_only_rejected_records_returns_nothing(monkeypatch):
    _serve(monkeypatch, [_record("E", ["Editorial"]), _record("N", ["News"])])

    assert semantic_scholar.search_topic("prompt injection") == []


def test_filtering_makes_no_extra_request(monkeypatch):
    """One search call, however many records are dropped — no pagination."""
    calls = _serve(monkeypatch, [_record(f"E{i}", ["Editorial"]) for i in range(20)])

    semantic_scholar.search_topic("prompt injection")

    assert len(calls) == 1
