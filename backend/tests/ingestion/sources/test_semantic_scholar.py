"""Tests for the Semantic Scholar ingestion source (Plan 01-03).

Metadata-only per README/D-14 — this source never downloads a PDF itself;
it only stores the `openAccessPdf.url` value for later enrichment. No test
makes a real network call to api.semanticscholar.org (D-23), and no test
sleeps: every rate limiter here runs on a fake clock.

httpx.get() (the module-level shortcut) does not accept a `transport`
kwarg — only httpx.Client() does — so the real client is a context-managed
httpx.Client(transport=...); tests fake that, not httpx.get directly.
"""

import inspect
import logging
import threading

import httpx
import pytest

from app.ingestion.rate_limit import CooldownActive
from app.ingestion.sources import semantic_scholar
from tests.ingestion.fake_clock import FakeClock


@pytest.fixture(autouse=True)
def _no_real_waiting(monkeypatch):
    """The process-wide limiter would otherwise make these tests sleep for real."""
    monkeypatch.setattr(semantic_scholar, "LIMITER", FakeClock().limiter())


class FakeResponse:
    """Stand-in for httpx.Response — only .status_code, .headers, .json() and
    .raise_for_status() are used."""

    def __init__(self, payload=None, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                str(self.status_code),
                request=httpx.Request("GET", semantic_scholar.SEARCH_URL),
                response=httpx.Response(self.status_code),
            )


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
            "citationCount": 412,
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


def _make_fake_client(captured):
    class FakeClient:
        def __init__(self, transport=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, headers=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            return FakeResponse(FIXTURE_BODY)

    return FakeClient


def test_search_topic_returns_corpus_shaped_dicts(monkeypatch):
    captured = {}
    monkeypatch.setattr(httpx, "Client", _make_fake_client(captured))

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
    captured = {}
    monkeypatch.setattr(httpx, "Client", _make_fake_client(captured))

    out = semantic_scholar.search_topic("graph neural networks", api_key=None)

    second = out[1]
    assert second["openaccess_pdf_url"] is None
    assert second["doi"] is None
    assert second["venue"] is None
    assert second["abstract"] is None
    assert second["authors"] == ["Carol Author"]


def test_search_topic_sends_api_key_header_when_provided(monkeypatch):
    captured = {}
    monkeypatch.setattr(httpx, "Client", _make_fake_client(captured))

    semantic_scholar.search_topic("graph neural networks", api_key="test-key-123")

    assert captured["headers"] == {"x-api-key": "test-key-123"}


def test_search_topic_sends_no_api_key_header_when_absent(monkeypatch):
    captured = {}
    monkeypatch.setattr(httpx, "Client", _make_fake_client(captured))

    semantic_scholar.search_topic("graph neural networks", api_key=None)

    assert captured["headers"] == {}
    assert "x-api-key" not in captured["headers"]


def test_source_is_metadata_only_never_calls_download_pdf(monkeypatch):
    source_text = inspect.getsource(semantic_scholar)
    assert "download_pdf" not in source_text

    captured = {}
    monkeypatch.setattr(httpx, "Client", _make_fake_client(captured))

    out = semantic_scholar.search_topic("graph neural networks", api_key=None)

    for paper in out:
        assert "pdf_path" not in paper
        assert "pdf_bytes" not in paper


def test_search_topic_carries_the_citation_count(monkeypatch):
    """Phase 8's `citation_impact` signal has no other source (README §7)."""
    monkeypatch.setattr(httpx, "Client", _make_fake_client({}))

    out = semantic_scholar.search_topic("graph neural networks", api_key=None)

    assert out[0]["citation_count"] == 412
    # Absent from the response rather than zero: an unknown count is not a
    # claim that nobody cited the paper.
    assert out[1]["citation_count"] is None


def test_search_topic_requests_the_citation_count_field(monkeypatch):
    captured = {}
    monkeypatch.setattr(httpx, "Client", _make_fake_client(captured))

    semantic_scholar.search_topic("graph neural networks", api_key=None)

    assert "citationCount" in captured["params"]["fields"].split(",")


def test_search_topic_sends_an_explicit_limit(monkeypatch):
    """Without `limit` the search endpoint returns up to 100 papers per call.

    The page is sized to `request_limit` (20 by default), a little more than
    the 10 kept, so non-research records dropped by publication type do not
    use up the source's cap (D-S2-TYPES-01).
    """
    captured = {}
    monkeypatch.setattr(httpx, "Client", _make_fake_client(captured))

    semantic_scholar.search_topic("graph neural networks", limit=25)
    assert captured["params"]["limit"] == 25

    semantic_scholar.search_topic("graph neural networks")
    assert captured["params"]["limit"] == 20


# ---------------------------------------------------------------------------
# Rate limiting: one process-wide limiter, bounded retry with backoff
# ---------------------------------------------------------------------------


def _make_sequenced_client(responses, calls):
    """A fake httpx.Client that answers each GET with the next queued response."""

    class SequencedClient:
        def __init__(self, transport=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, headers=None, timeout=None):
            calls.append({"params": params, "headers": headers})
            return responses[len(calls) - 1]

    return SequencedClient


def _search(monkeypatch, responses, **kwargs):
    fake = FakeClock()
    calls = []
    monkeypatch.setattr(httpx, "Client", _make_sequenced_client(responses, calls))
    options = {"min_interval_seconds": 0.0, "limiter": fake.limiter()} | kwargs
    out = semantic_scholar.search_topic("graph neural networks", **options)
    return out, calls, fake.sleeps


def test_requests_from_separate_calls_are_spaced_by_the_shared_limiter(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(semantic_scholar, "LIMITER", fake.limiter())
    monkeypatch.setattr(httpx, "Client", _make_fake_client({}))

    semantic_scholar.search_topic("first", min_interval_seconds=1.5)
    semantic_scholar.search_topic("second", min_interval_seconds=1.5)

    assert fake.sleeps == [1.5]


def test_concurrent_callers_share_one_semantic_scholar_limiter(monkeypatch):
    fake = FakeClock(frozen=True)
    monkeypatch.setattr(semantic_scholar, "LIMITER", fake.limiter())
    monkeypatch.setattr(httpx, "Client", _make_fake_client({}))
    barrier = threading.Barrier(5)
    errors: list[Exception] = []

    def _caller():
        barrier.wait()
        try:
            semantic_scholar.search_topic("topic", min_interval_seconds=2.0)
        except Exception as exc:  # noqa: BLE001 — a thread's error is otherwise lost
            errors.append(exc)

    threads = [threading.Thread(target=_caller) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(fake.sleeps) == [2.0, 4.0, 6.0, 8.0]


def test_search_topic_retries_a_429_then_returns_results(monkeypatch):
    out, calls, sleeps = _search(
        monkeypatch,
        [FakeResponse(status_code=429), FakeResponse(FIXTURE_BODY)],
        backoff_base_seconds=1.0,
    )

    assert len(out) == 2
    assert len(calls) == 2
    assert sleeps == [1.0]


def test_search_topic_retries_a_transient_server_error(monkeypatch):
    out, calls, _ = _search(
        monkeypatch,
        [FakeResponse(status_code=503), FakeResponse(FIXTURE_BODY)],
    )

    assert len(out) == 2
    assert len(calls) == 2


def test_search_topic_retry_waits_for_the_minimum_interval(monkeypatch):
    """A retry is a request like any other: it queues behind the spacing."""
    _, _, sleeps = _search(
        monkeypatch,
        [FakeResponse(status_code=429), FakeResponse(FIXTURE_BODY)],
        min_interval_seconds=1.5,
        backoff_base_seconds=1.0,
    )

    assert sleeps == [1.5]


def test_search_topic_honours_retry_after(monkeypatch):
    _, _, sleeps = _search(
        monkeypatch,
        [
            FakeResponse(status_code=429, headers={"Retry-After": "3"}),
            FakeResponse(FIXTURE_BODY),
        ],
        backoff_base_seconds=1.0,
    )

    assert sleeps == [3.0]


def test_search_topic_backoff_doubles_between_attempts(monkeypatch):
    _, _, sleeps = _search(
        monkeypatch,
        [FakeResponse(status_code=429)] * 3 + [FakeResponse(FIXTURE_BODY)],
        max_retries=3,
        backoff_base_seconds=2.0,
        backoff_max_seconds=30.0,
    )

    assert sleeps == [2.0, 4.0, 8.0]


def test_search_topic_gives_up_rather_than_retry_before_retry_after(monkeypatch):
    """Retrying before the server's Retry-After would be the violation itself."""
    fake = FakeClock()
    limiter = fake.limiter()
    calls = []
    monkeypatch.setattr(
        httpx,
        "Client",
        _make_sequenced_client(
            [
                FakeResponse(status_code=429, headers={"Retry-After": "120"}),
                FakeResponse(FIXTURE_BODY),
            ],
            calls,
        ),
    )
    options = {
        "limiter": limiter,
        "min_interval_seconds": 0.0,
        "backoff_max_seconds": 30.0,
    }

    with pytest.raises(httpx.HTTPStatusError):
        semantic_scholar.search_topic("graph neural networks", **options)

    assert len(calls) == 1
    assert fake.sleeps == []
    # The cooldown is remembered for the whole process: the next call sends nothing.
    with pytest.raises(CooldownActive):
        semantic_scholar.search_topic("graph neural networks", **options)
    assert len(calls) == 1


def test_search_topic_stops_before_a_backoff_would_exceed_the_cap(monkeypatch):
    fake = FakeClock()
    calls = []
    monkeypatch.setattr(
        httpx,
        "Client",
        _make_sequenced_client([FakeResponse(status_code=429)] * 5, calls),
    )

    with pytest.raises(httpx.HTTPStatusError):
        semantic_scholar.search_topic(
            "graph neural networks",
            limiter=fake.limiter(),
            min_interval_seconds=0.0,
            max_retries=4,
            backoff_base_seconds=1.0,
            backoff_max_seconds=5.0,
        )

    assert len(calls) == 4  # the fourth backoff (8s) would exceed the 5s cap
    assert fake.sleeps == [1.0, 2.0, 4.0]


def test_search_topic_raises_once_retries_are_exhausted(monkeypatch):
    fake = FakeClock()
    calls = []
    monkeypatch.setattr(
        httpx,
        "Client",
        _make_sequenced_client([FakeResponse(status_code=429)] * 4, calls),
    )

    with pytest.raises(httpx.HTTPStatusError):
        semantic_scholar.search_topic(
            "graph neural networks",
            limiter=fake.limiter(),
            min_interval_seconds=0.0,
            max_retries=3,
            backoff_base_seconds=1.0,
        )

    assert len(calls) == 4  # the first try plus three retries
    assert len(fake.sleeps) == 3  # no pointless wait after the final attempt


def test_search_topic_fails_immediately_on_a_non_retryable_error(monkeypatch):
    """A 403 (bad key) will not fix itself; retrying only burns the rate limit."""
    fake = FakeClock()
    calls = []
    monkeypatch.setattr(
        httpx, "Client", _make_sequenced_client([FakeResponse(status_code=403)], calls)
    )

    with pytest.raises(httpx.HTTPStatusError):
        semantic_scholar.search_topic(
            "graph neural networks", limiter=fake.limiter(), min_interval_seconds=0.0
        )

    assert len(calls) == 1
    assert fake.sleeps == []


def test_the_api_key_never_reaches_logs_or_errors(monkeypatch, caplog):
    secret = "s2-secret-key-never-log-me"
    fake = FakeClock()
    calls = []
    monkeypatch.setattr(
        httpx,
        "Client",
        _make_sequenced_client([FakeResponse(status_code=429)] * 2, calls),
    )

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(httpx.HTTPStatusError) as excinfo,
    ):
        semantic_scholar.search_topic(
            "graph neural networks",
            api_key=secret,
            limiter=fake.limiter(),
            min_interval_seconds=0.0,
            max_retries=1,
            backoff_base_seconds=1.0,
        )

    assert all(call["headers"] == {"x-api-key": secret} for call in calls)
    assert "retry" in caplog.text.lower()  # the retry was logged...
    assert secret not in caplog.text  # ...without the key
    assert secret not in str(excinfo.value)


def test_search_topic_keeps_arxiv_and_acl_identifiers_for_dedup(monkeypatch):
    """S1 audit: Semantic Scholar's copy of an arXiv paper was not recognised
    because only `externalIds.DOI` survived the mapping."""
    body = {
        "data": [
            {
                "title": "T",
                "authors": [],
                "externalIds": {
                    "DOI": "10.18653/v1/2024.wmt-1.30",
                    "ArXiv": "2410.00001",
                    "ACL": "2024.wmt-1.30",
                },
            },
            {"title": "U", "authors": [], "externalIds": {"DOI": "10.1/x"}},
        ]
    }

    class FakeClient:
        def __init__(self, transport=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, headers=None, timeout=None):
            return FakeResponse(body)

    monkeypatch.setattr(httpx, "Client", FakeClient)

    first, second = semantic_scholar.search_topic("t", api_key=None)

    assert first["arxiv_id"] == "2410.00001"
    assert first["acl_id"] == "2024.wmt-1.30"
    assert second["arxiv_id"] is None and second["acl_id"] is None


# ---------------------------------------------------------------------------
# Paging, backfill and provenance (D-S1-RETRIEVAL-01, 2026-10-12)
#
# Production read one page of 40 from a search Semantic Scholar said matched
# 16,451, ignored `total`, and dropped `openAccessPdf`. The search now follows
# `next` within a bounded pool and page budget, backfills dropped records,
# reports how far it read, and keeps the open-access link and provenance.
# ---------------------------------------------------------------------------


def _paper(n, **extra):
    return {
        "paperId": f"p{n}",
        "url": f"https://www.semanticscholar.org/paper/p{n}",
        "title": f"Paper {n}",
        "authors": [{"name": "A"}],
        "externalIds": {"DOI": f"10.1/{n}"},
        "abstract": "Abstract.",
        **extra,
    }


def _page(papers, *, offset, total, next_offset=None):
    body = {"total": total, "offset": offset, "data": papers}
    if next_offset is not None:
        body["next"] = next_offset
    return FakeResponse(body)


def _paged(monkeypatch, responses, **kwargs):
    out, calls, _ = _search(monkeypatch, responses, **kwargs)
    return out, [c["params"] for c in calls]


def test_results_beyond_the_first_page_are_returned(monkeypatch):
    out, params = _paged(
        monkeypatch,
        [
            _page([_paper(1), _paper(2)], offset=0, total=5, next_offset=2),
            _page([_paper(3), _paper(4)], offset=2, total=5, next_offset=4),
            _page([_paper(5)], offset=4, total=5),
        ],
        limit=10,
        page_size=2,
        max_pages=5,
    )

    assert [c["title"] for c in out] == [f"Paper {n}" for n in range(1, 6)]
    assert [p["offset"] for p in params] == [0, 2, 4]
    assert all(p["limit"] == 2 for p in params)
    assert out.retrieval["total_reported"] == 5
    assert out.retrieval["pages_requested"] == 3
    assert out.retrieval["complete"] is True


def test_dropped_records_are_backfilled_from_the_next_page(monkeypatch):
    editorial = _paper(2, publicationTypes=["Editorial"])
    out, params = _paged(
        monkeypatch,
        [
            _page([_paper(1), editorial], offset=0, total=10, next_offset=2),
            _page([_paper(3), _paper(4)], offset=2, total=10, next_offset=4),
        ],
        limit=3,
        page_size=2,
        max_pages=5,
    )

    assert [c["title"] for c in out] == ["Paper 1", "Paper 3", "Paper 4"]
    assert len(params) == 2
    assert out.retrieval["records_dropped"] == {"non_research": 1}


def test_reaching_the_pool_before_the_total_is_reported_incomplete(monkeypatch):
    out, params = _paged(
        monkeypatch,
        [_page([_paper(1), _paper(2)], offset=0, total=16451, next_offset=2)],
        limit=2,
        page_size=2,
    )

    assert len(out) == 2
    assert len(params) == 1
    assert out.retrieval["stop_reason"] == "ceiling"
    assert out.retrieval["complete"] is False
    assert out.retrieval["total_reported"] == 16451


def test_a_paper_repeated_on_a_later_page_is_kept_once(monkeypatch):
    out, _ = _paged(
        monkeypatch,
        [
            _page([_paper(1), _paper(2)], offset=0, total=4, next_offset=2),
            _page([_paper(2), _paper(3)], offset=2, total=4),
        ],
        limit=10,
        page_size=2,
        max_pages=5,
    )

    assert [c["s2_paper_id"] for c in out] == ["p1", "p2", "p3"]
    assert out.retrieval["records_dropped"] == {"duplicate_across_pages": 1}


def test_a_failed_later_page_keeps_earlier_results_and_is_visible(monkeypatch):
    out, _ = _paged(
        monkeypatch,
        [
            _page([_paper(1), _paper(2)], offset=0, total=10, next_offset=2),
            FakeResponse({"error": "bad"}, status_code=404),
        ],
        limit=10,
        page_size=2,
        max_pages=5,
    )

    assert len(out) == 2
    assert out.retrieval["stop_reason"] == "page_error"
    assert out.retrieval["error"] == "http_404"
    assert out.retrieval["complete"] is False


def test_a_malformed_later_page_is_reported_incomplete(monkeypatch):
    out, _ = _paged(
        monkeypatch,
        [
            _page([_paper(1)], offset=0, total=10, next_offset=1),
            FakeResponse(["not", "an", "object"]),
        ],
        limit=10,
        page_size=1,
        max_pages=5,
    )

    assert len(out) == 1
    assert out.retrieval["error"] == "TypeError"


def test_the_page_budget_bounds_the_requests(monkeypatch):
    out, params = _paged(
        monkeypatch,
        [
            _page([_paper(1)], offset=0, total=100, next_offset=1),
            _page([_paper(2)], offset=1, total=100, next_offset=2),
        ],
        limit=50,
        page_size=1,
        max_pages=2,
    )

    assert len(params) == 2
    assert out.retrieval["stop_reason"] == "page_budget"
    assert out.retrieval["complete"] is False


def test_paging_never_asks_past_the_apis_thousand_result_ceiling(monkeypatch):
    pages = [
        _page(
            [_paper(i * 100 + j) for j in range(100)],
            offset=i * 100,
            total=16451,
            next_offset=(i + 1) * 100,
        )
        for i in range(10)
    ]
    out, params = _paged(monkeypatch, pages, limit=5000, page_size=100, max_pages=50)

    assert len(params) == 10
    assert max(p["offset"] + p["limit"] for p in params) == 1000
    assert len(out) == 1000
    assert out.retrieval["stop_reason"] == "ceiling"


def test_a_record_without_a_title_is_dropped(monkeypatch):
    out, _ = _paged(
        monkeypatch,
        [_page([_paper(1, title=None), _paper(2)], offset=0, total=2)],
        limit=10,
    )

    assert [c["title"] for c in out] == ["Paper 2"]
    assert out.retrieval["records_dropped"] == {"no_title": 1}


def test_the_open_access_link_and_provenance_are_kept(monkeypatch):
    oa = {"url": "https://arxiv.org/pdf/2409.13331", "status": "GREEN", "license": None}
    none = {"url": "", "status": "CLOSED", "license": None}
    out, _ = _paged(
        monkeypatch,
        [
            _page(
                [_paper(1, openAccessPdf=oa), _paper(2, openAccessPdf=none)],
                offset=0,
                total=2,
            )
        ],
        limit=10,
    )

    assert out[0]["openaccess_pdf_url"] == "https://arxiv.org/pdf/2409.13331"
    assert out[0]["openaccess_status"] == "GREEN"
    assert out[0]["s2_paper_id"] == "p1"
    assert out[0]["source_url"] == "https://www.semanticscholar.org/paper/p1"
    # The API's "no copy" answer is an empty string, not a URL.
    assert out[1]["openaccess_pdf_url"] is None
    assert out[1]["openaccess_status"] == "CLOSED"


def test_the_search_asks_for_the_provenance_fields(monkeypatch):
    _, params = _paged(monkeypatch, [_page([], offset=0, total=0)])

    fields = params[0]["fields"].split(",")
    assert {"paperId", "url", "openAccessPdf", "externalIds"} <= set(fields)


def test_no_results_is_a_complete_empty_read(monkeypatch):
    out, params = _paged(monkeypatch, [_page([], offset=0, total=0)])

    assert out == []
    assert len(params) == 1
    assert out.retrieval["complete"] is True
