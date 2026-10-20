"""Tests for the arXiv ingestion source (Plan 01-02).

Per D-13 (corrected): the installed `arxiv` v4.0.1 package has no
`download_pdf`/`download_source` helper on `Result` — PDFs must be fetched
via the shared `corpus.download_pdf` httpx helper against `Result.pdf_url`.
No test makes a real network call to arxiv.org (D-23): request-level tests
mount a fake `requests` adapter, and every rate limiter runs on a fake clock.
"""

import io
import logging
import threading
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

import arxiv
import pytest
import requests
from requests.structures import CaseInsensitiveDict

from app.ingestion.rate_limit import CooldownActive
from app.ingestion.sources import arxiv as arxiv_source
from tests.ingestion.fake_clock import FakeClock


@pytest.fixture(autouse=True)
def _no_real_waiting(monkeypatch):
    """The process-wide limiter would otherwise make these tests sleep for real."""
    monkeypatch.setattr(arxiv_source, "LIMITER", FakeClock().limiter())


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


class _Header:
    def __init__(self, total):
        self.total_results = total


class _Feed:
    """Stand-in for the arxiv library's ParsedFeed: one page of results."""

    def __init__(self, results, total, malformed=False):
        self.results = list(results)
        self.header = _Header(total)
        self.malformed = malformed


def _serve_pages(monkeypatch, *pages):
    """Answer each page request with the next queued `_Feed` (or raise it).

    Returns the query parameters of every page requested, in order.
    """
    requested: list[dict] = []

    def fake_parse_feed(self, url, first_page=True, _try_index=0):
        requested.append(parse_qs(urlparse(url).query))
        page = pages[len(requested) - 1]
        if isinstance(page, BaseException):
            raise page
        return page

    monkeypatch.setattr(arxiv_source.arxiv.Client, "_parse_feed", fake_parse_feed)
    return requested


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
    _serve_pages(monkeypatch, _Feed(results, total=2))

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
    _serve_pages(monkeypatch, _Feed([make_result(doi=None)], total=1))

    out = arxiv_source.search_topic("transformer models")

    assert len(out) == 1
    assert out[0]["doi"] is None
    assert out[0]["arxiv_id"] == "1706.03762"


class RecordingClient(arxiv.Client):
    """The real arxiv.Client, recording how it was configured."""

    last: ClassVar["RecordingClient | None"] = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.kwargs = kwargs
        RecordingClient.last = self


def test_the_page_size_defaults_to_the_ceiling(monkeypatch):
    """arxiv.Client's own page size (100) is never what is requested: one
    request asks for the whole ceiling unless a page size is configured."""
    monkeypatch.setattr(arxiv_source.arxiv, "Client", RecordingClient)
    adapter = FakeAdapter([ok()])

    _search(adapter, max_results=7)

    assert RecordingClient.last.kwargs["page_size"] == 7
    assert parse_qs(urlparse(adapter.sent[0].url).query)["max_results"] == ["7"]


def test_search_topic_defaults_to_ten_results(monkeypatch):
    monkeypatch.setattr(arxiv_source.arxiv, "Client", RecordingClient)

    _search(FakeAdapter([ok()]))

    assert RecordingClient.last.kwargs["page_size"] == 10


def test_the_library_retry_and_delay_layers_are_switched_off(monkeypatch):
    """ScholarLens owns spacing and retries. A second retry layer inside the
    library would retry the same 429 again, and its delay resets with every
    new Client, so it paces nothing across calls."""
    monkeypatch.setattr(arxiv_source.arxiv, "Client", RecordingClient)

    _search(FakeAdapter([ok()]), num_retries=3)

    client = RecordingClient.last
    assert client.kwargs["num_retries"] == 0
    assert client.kwargs["delay_seconds"] == 0
    assert isinstance(client._session, arxiv_source.PoliteSession)


def test_fetch_pdf_returns_none_on_download_failure(monkeypatch):
    monkeypatch.setattr(
        arxiv_source, "download_pdf", lambda url, dest_path, **kwargs: False
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

    _serve_pages(monkeypatch, _Feed([BareResult()], total=1))

    out = arxiv_source.search_topic("anything")

    assert len(out) == 1
    assert out[0]["arxiv_id"] == "9999.99999"


# ---------------------------------------------------------------------------
# Paging (D-S1-RETRIEVAL-01, 2026-10-12)
#
# Production read one page of 30 from a query arXiv said matched 59. The
# search now pages until arXiv's own total is exhausted or the retrieval
# ceiling is reached, and reports which -- never presenting a partial read as
# exhaustive.
# ---------------------------------------------------------------------------


def _results(*ids):
    return [make_result(short_id=i, title=f"Paper {i}") for i in ids]


def test_results_beyond_the_first_page_are_returned(monkeypatch):
    requested = _serve_pages(
        monkeypatch,
        _Feed(_results("1", "2"), total=5),
        _Feed(_results("3", "4"), total=5),
        _Feed(_results("5"), total=5),
    )

    out = arxiv_source.search_topic("t", max_results=10, page_size=2)

    assert [c["arxiv_id"] for c in out] == ["1", "2", "3", "4", "5"]
    assert [r["start"] for r in requested] == [["0"], ["2"], ["4"]]
    assert out.retrieval["total_reported"] == 5
    assert out.retrieval["pages_requested"] == 3
    assert out.retrieval["records_fetched"] == 5
    assert out.retrieval["complete"] is True
    assert out.retrieval["stop_reason"] == "exhausted"


def test_no_page_is_requested_after_the_reported_total(monkeypatch):
    requested = _serve_pages(monkeypatch, _Feed(_results("1", "2"), total=2))

    out = arxiv_source.search_topic("t", max_results=10, page_size=2)

    assert len(requested) == 1
    assert out.retrieval["complete"] is True


def test_the_ceiling_stops_the_read_and_is_reported_incomplete(monkeypatch):
    requested = _serve_pages(
        monkeypatch,
        _Feed(_results("1", "2"), total=50),
        _Feed(_results("3", "4"), total=50),
    )

    out = arxiv_source.search_topic("t", max_results=4, page_size=2)

    assert len(out) == 4
    assert len(requested) == 2
    assert out.retrieval["stop_reason"] == "ceiling"
    assert out.retrieval["complete"] is False
    assert out.retrieval["total_reported"] == 50


def test_a_paper_repeated_on_a_later_page_is_kept_once(monkeypatch):
    """arXiv's relevance order can shift between page requests; the same paper
    (any version) must not become two candidates."""
    _serve_pages(
        monkeypatch,
        _Feed(_results("2401.00001v1", "2401.00002v1"), total=4),
        _Feed(_results("2401.00001v2", "2401.00003v1"), total=4),
    )

    out = arxiv_source.search_topic("t", max_results=10, page_size=2)

    assert [c["arxiv_id"] for c in out] == [
        "2401.00001v1",
        "2401.00002v1",
        "2401.00003v1",
    ]
    assert out.retrieval["records_fetched"] == 4
    assert out.retrieval["records_dropped"] == {"duplicate_across_pages": 1}


def test_a_failed_later_page_keeps_earlier_results_and_is_visible(monkeypatch):
    _serve_pages(
        monkeypatch,
        _Feed(_results("1", "2"), total=6),
        arxiv.HTTPError("https://export.arxiv.org/api/query", 0, 503),
    )

    out = arxiv_source.search_topic("t", max_results=10, page_size=2)

    assert [c["arxiv_id"] for c in out] == ["1", "2"]
    assert out.retrieval["stop_reason"] == "page_error"
    assert out.retrieval["error"] == "http_503"
    assert out.retrieval["pages_requested"] == 2
    assert out.retrieval["pages_succeeded"] == 1
    assert out.retrieval["complete"] is False


def test_an_empty_later_page_is_reported_incomplete(monkeypatch):
    _serve_pages(
        monkeypatch,
        _Feed(_results("1", "2"), total=6),
        arxiv.UnexpectedEmptyPageError("u", 0, _Feed([], total=6)),
    )

    out = arxiv_source.search_topic("t", max_results=10, page_size=2)

    assert len(out) == 2
    assert out.retrieval["stop_reason"] == "empty_page"
    assert out.retrieval["complete"] is False


def test_a_malformed_later_page_is_reported_incomplete(monkeypatch):
    _serve_pages(
        monkeypatch,
        _Feed(_results("1", "2"), total=6),
        _Feed([], total=0, malformed=True),
    )

    out = arxiv_source.search_topic("t", max_results=10, page_size=2)

    assert len(out) == 2
    assert out.retrieval["error"] == "malformed_feed"
    assert out.retrieval["complete"] is False


def test_a_failed_first_page_still_fails_the_source(monkeypatch):
    _serve_pages(monkeypatch, arxiv.HTTPError("u", 0, 503))

    with pytest.raises(arxiv.HTTPError):
        arxiv_source.search_topic("t", max_results=10, page_size=2)


def test_a_malformed_first_page_fails_the_source(monkeypatch):
    _serve_pages(monkeypatch, _Feed([], total=0, malformed=True))

    with pytest.raises(ValueError, match="malformed"):
        arxiv_source.search_topic("t")


def test_no_results_is_a_complete_empty_read(monkeypatch):
    _serve_pages(monkeypatch, _Feed([], total=0))

    out = arxiv_source.search_topic("t")

    assert out == []
    assert out.retrieval["complete"] is True
    assert out.retrieval["total_reported"] == 0


def test_the_report_records_the_query_and_a_stable_fingerprint(monkeypatch):
    _serve_pages(monkeypatch, _Feed([], total=0), _Feed([], total=0))

    first = arxiv_source.search_topic("prompt injection").retrieval
    second = arxiv_source.search_topic("prompt injection").retrieval

    assert first["query"] == 'all:"prompt" AND all:"injection"'
    assert first["query_fingerprint"] == second["query_fingerprint"]
    assert first["params"] == {"ceiling": 10, "page_size": 10}


def test_every_page_request_waits_its_turn_on_the_limiter():
    fake = FakeClock()
    page = (
        FEED_HEAD
        + b"<opensearch:totalResults>2</opensearch:totalResults>"
        + ONE_RESULT_FEED.split(b"</opensearch:totalResults>", 1)[1]
    )
    second = page.replace(b"2401.00001", b"2401.00002")
    adapter = FakeAdapter([ok(page), ok(second)])

    out = _search(adapter, fake=fake, max_results=10, page_size=1)

    assert len(out) == 2
    assert len(adapter.sent) == 2
    assert fake.sleeps == [3.0]


# ---------------------------------------------------------------------------
# Request level: User-Agent, pacing and 429 handling through the real
# arxiv.Client, served by a fake adapter instead of the network
# ---------------------------------------------------------------------------

FEED_HEAD = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<feed xmlns="http://www.w3.org/2005/Atom"'
    b' xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"'
    b' xmlns:arxiv="http://arxiv.org/schemas/atom">'
)
EMPTY_FEED = FEED_HEAD + b"<opensearch:totalResults>0</opensearch:totalResults></feed>"
ONE_RESULT_FEED = (
    FEED_HEAD
    + b"<opensearch:totalResults>1</opensearch:totalResults>"
    + b"<entry>"
    + b"<id>http://arxiv.org/abs/2401.00001v1</id>"
    + b"<updated>2024-01-02T00:00:00Z</updated>"
    + b"<published>2024-01-02T00:00:00Z</published>"
    + b"<title>Prompt Injection in the Wild</title>"
    + b"<summary>An abstract.</summary>"
    + b"<author><name>Ada Author</name></author>"
    + b'<link href="http://arxiv.org/pdf/2401.00001v1" rel="related"'
    + b' type="application/pdf" title="pdf"/>'
    + b'<arxiv:primary_category term="cs.CR"/>'
    + b"</entry></feed>"
)


class FakeAdapter(requests.adapters.BaseAdapter):
    """Serves queued answers instead of the network.

    Each queued answer is either a `(status, body, headers)` tuple, or an
    exception instance to raise instead of returning a response — standing in
    for a transport-level failure (read timeout, connection error) that never
    produces a status code.
    """

    def __init__(self, answers):
        super().__init__()
        self._answers = list(answers)
        self.sent: list[requests.PreparedRequest] = []
        self._lock = threading.Lock()

    def send(self, request, **kwargs):
        with self._lock:
            self.sent.append(request)
            answer = self._answers[len(self.sent) - 1]
        if isinstance(answer, BaseException):
            raise answer
        status, body, headers = answer
        response = requests.Response()
        response.status_code = status
        response._content = body
        response.raw = io.BytesIO(body)
        response.headers = CaseInsensitiveDict(headers)
        response.url = request.url
        response.request = request
        return response

    def close(self):
        pass


def ok(body=EMPTY_FEED):
    return (200, body, {})


def throttled(retry_after=None):
    return (429, b"Rate exceeded.", {"Retry-After": retry_after} if retry_after else {})


def _search(adapter, fake=None, topic="prompt injection", **kwargs):
    fake = fake or FakeClock()
    options = {
        "min_interval_seconds": 3.0,
        "num_retries": 2,
        "backoff_base_seconds": 10.0,
        "backoff_max_seconds": 60.0,
        "limiter": fake.limiter(),
        "adapter": adapter,
    } | kwargs
    return arxiv_source.search_topic(topic, **options)


def test_requests_carry_a_descriptive_scholarlens_user_agent():
    adapter = FakeAdapter([ok()])

    _search(adapter, contact="ops@example.org")

    agent = adapter.sent[0].headers["User-Agent"]
    assert agent.startswith("ScholarLens/")
    assert "ops@example.org" in agent
    assert "arxiv.py" not in agent


def test_the_user_agent_omits_the_contact_when_none_is_configured():
    adapter = FakeAdapter([ok()])

    _search(adapter, contact=None)

    agent = adapter.sent[0].headers["User-Agent"]
    assert agent.startswith("ScholarLens/")
    assert "contact" not in agent


def test_the_request_asks_arxiv_for_exactly_the_configured_result_limit():
    adapter = FakeAdapter([ok()])

    _search(adapter, max_results=10)

    query = parse_qs(urlparse(adapter.sent[0].url).query)
    assert query["max_results"] == ["10"]
    assert query["search_query"] == ['all:"prompt" AND all:"injection"']


# ---------------------------------------------------------------------------
# Query construction (D-QUERY-02, superseding D-QUERY-01)
#
# D-QUERY-01 sent the topic as one all-fields *phrase* — `all:"prompt
# injection"` — which requires those words adjacent, in that order, in one
# field. That is far narrower than a researcher means by a topic: "multilingual
# prompt injection" returned nothing from arXiv while Semantic Scholar, which
# tokenizes the query itself, returned ten papers. The topic is now split by
# `app.ingestion.topic.meaningful_terms` and sent as an AND of its terms, each
# still an all-fields phrase of its own. A paper must contain every meaningful
# term somewhere — so the search is still constrained by everything the user
# typed — but the terms no longer have to sit next to each other.
# ---------------------------------------------------------------------------


def _sent_query(adapter) -> str:
    return parse_qs(urlparse(adapter.sent[0].url).query)["search_query"][0]


def test_a_multi_word_topic_is_sent_as_an_and_of_its_terms():
    adapter = FakeAdapter([ok()])

    _search(adapter, topic="Multilingual prompt injection")

    assert _sent_query(adapter) == (
        'all:"Multilingual" AND all:"prompt" AND all:"injection"'
    )


def test_a_single_word_topic_is_still_a_single_all_fields_query():
    adapter = FakeAdapter([ok()])

    _search(adapter, topic="transformers")

    assert _sent_query(adapter) == 'all:"transformers"'


def test_the_query_is_not_broadened_into_an_or_of_its_terms():
    """Every term is still required. Dropping to OR would return papers about
    prompting in general, which is the noisy search this change must not make."""
    adapter = FakeAdapter([ok()])

    _search(adapter)

    assert " OR " not in _sent_query(adapter)
    assert _sent_query(adapter).count("AND") == 1


def test_function_words_do_not_constrain_the_query():
    adapter = FakeAdapter([ok()])

    _search(adapter, topic="the alignment tax")

    assert _sent_query(adapter) == 'all:"alignment" AND all:"tax"'


def test_the_url_is_encoded_rather_than_carrying_raw_spaces_or_quotes():
    """`search_query` is percent-encoded, so the topic survives the round trip."""
    adapter = FakeAdapter([ok()])

    _search(adapter)

    assert " " not in adapter.sent[0].url
    assert '"' not in adapter.sent[0].url


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        (
            "retrieval-augmented generation",
            'all:"retrieval-augmented" AND all:"generation"',
        ),
        ("GPT-4 (vision)", 'all:"GPT-4" AND all:"vision"'),
        ("hate speech: a survey", 'all:"hate" AND all:"speech" AND all:"survey"'),
        (
            "back-translation & paraphrase",
            'all:"back-translation" AND all:"paraphrase"',
        ),
        ("Müller's method", 'all:"Müller\'s" AND all:"method"'),
    ],
)
def test_punctuation_in_a_topic_survives_the_round_trip(topic, expected):
    """Hyphens, apostrophes and non-ASCII stay inside a term as the user typed
    them; only the separators arXiv's own grammar would read — parentheses,
    colons, ampersands — are dropped."""
    adapter = FakeAdapter([ok()])

    _search(adapter, topic=topic)

    assert _sent_query(adapter) == expected


def test_double_quotes_inside_a_topic_cannot_break_out_of_the_phrase():
    """arXiv's grammar has no documented escape for a quote inside a quoted
    phrase, so embedded quotes are dropped rather than sent as-is, where they
    would terminate a term's phrase early and change the query's meaning."""
    adapter = FakeAdapter([ok()])

    _search(adapter, topic='the "alignment tax"')

    assert _sent_query(adapter) == 'all:"alignment" AND all:"tax"'


def test_relevance_sorting_and_descending_order_are_unchanged():
    adapter = FakeAdapter([ok()])

    _search(adapter)

    query = parse_qs(urlparse(adapter.sent[0].url).query)
    assert query["sortBy"] == ["relevance"]
    assert query["sortOrder"] == ["descending"]


def test_a_full_request_maps_the_feed_onto_corpus_dicts():
    out = _search(FakeAdapter([ok(ONE_RESULT_FEED)]))

    assert [paper["title"] for paper in out] == ["Prompt Injection in the Wild"]
    assert out[0]["arxiv_id"] == "2401.00001v1"


def test_requests_from_separate_calls_are_spaced_by_the_shared_limiter(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(arxiv_source, "LIMITER", fake.limiter())

    for topic in ("first", "second"):
        arxiv_source.search_topic(
            topic, min_interval_seconds=5.0, adapter=FakeAdapter([ok()])
        )

    assert fake.sleeps == [5.0]


def test_concurrent_callers_share_one_arxiv_limiter(monkeypatch):
    fake = FakeClock(frozen=True)
    monkeypatch.setattr(arxiv_source, "LIMITER", fake.limiter())
    barrier = threading.Barrier(4)
    errors: list[Exception] = []

    def _caller():
        barrier.wait()
        try:
            arxiv_source.search_topic(
                "topic", min_interval_seconds=5.0, adapter=FakeAdapter([ok()])
            )
        except Exception as exc:  # noqa: BLE001 — a thread's error is otherwise lost
            errors.append(exc)

    threads = [threading.Thread(target=_caller) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(fake.sleeps) == [5.0, 10.0, 15.0]


def test_a_429_is_retried_after_backoff_not_immediately():
    """A 429 that names a Retry-After is worth waiting out (D-429-03)."""
    fake = FakeClock()
    adapter = FakeAdapter([throttled("5"), ok()])

    _search(adapter, fake=fake)

    assert len(adapter.sent) == 2
    assert fake.sleeps == [10.0]  # max(Retry-After 5, backoff 10)


def test_retry_after_is_honoured():
    fake = FakeClock()

    _search(FakeAdapter([throttled("25"), ok()]), fake=fake)

    assert fake.sleeps == [25.0]


def test_backoff_doubles_between_attempts():
    fake = FakeClock()

    _search(FakeAdapter([throttled("1"), throttled("1"), ok()]), fake=fake)

    assert fake.sleeps == [10.0, 20.0]


def test_a_retry_waits_for_the_minimum_interval():
    """A backoff shorter than the spacing does not let a retry jump the queue."""
    fake = FakeClock()

    _search(
        FakeAdapter([throttled("1"), ok()]),
        fake=fake,
        min_interval_seconds=15.0,
        backoff_base_seconds=10.0,
    )

    assert fake.sleeps == [15.0]


def test_retries_are_bounded_and_the_429_surfaces_as_a_clean_source_error():
    fake = FakeClock()
    adapter = FakeAdapter([throttled("1")] * 5)

    with pytest.raises(arxiv.HTTPError) as excinfo:
        _search(adapter, fake=fake, num_retries=2)

    assert excinfo.value.status == 429
    # One retry layer only: the first try plus two retries — not multiplied by
    # a second set of retries inside the library.
    assert len(adapter.sent) == 3
    assert fake.sleeps == [10.0, 20.0]


def test_a_retry_after_beyond_the_cap_fails_fast_and_is_remembered():
    fake = FakeClock()
    limiter = fake.limiter()
    first = FakeAdapter([throttled("600"), ok()])

    with pytest.raises(arxiv.HTTPError):
        _search(first, fake=fake, limiter=limiter, backoff_max_seconds=60.0)

    assert len(first.sent) == 1
    assert fake.sleeps == []
    # The next call during the server's cooldown sends nothing at all.
    second = FakeAdapter([ok()])
    with pytest.raises(CooldownActive):
        _search(second, fake=fake, limiter=limiter, backoff_max_seconds=60.0)
    assert second.sent == []


# ---------------------------------------------------------------------------
# A 429 with no Retry-After: one request per run (D-429-03)
#
# Live arXiv answers `429 Rate exceeded.` with no Retry-After and no rate-limit
# headers, from an IP-level block that outlives any backoff we could compute.
# Retrying it sent three requests per /collect to a host that was refusing all
# of them and added ~30s to every run. It is now treated as "this source is
# unavailable for now": one request, source reported failed, run continues.
# ---------------------------------------------------------------------------


def test_a_429_without_retry_after_makes_exactly_one_arxiv_request():
    """The headline guarantee: one metadata request per collection run."""
    fake = FakeClock()
    adapter = FakeAdapter([throttled()] * 5)

    with pytest.raises(arxiv.HTTPError) as excinfo:
        _search(adapter, fake=fake, num_retries=2)

    assert excinfo.value.status == 429
    assert len(adapter.sent) == 1
    assert fake.sleeps == []


def test_declining_to_retry_parks_arxiv_for_the_backoff_ceiling():
    """Not retrying is not an excuse to come back sooner: the shared limiter is
    held for the full backoff ceiling, longer than the retry path deferred."""
    fake = FakeClock()
    limiter = fake.limiter()

    with pytest.raises(arxiv.HTTPError):
        _search(
            FakeAdapter([throttled()]),
            fake=fake,
            limiter=limiter,
            backoff_max_seconds=60.0,
        )
    limiter.acquire()

    assert fake.sleeps == [60.0]


def test_a_429_without_retry_after_makes_exactly_one_pdf_request(tmp_path):
    """The PDF path shares the session, so it shares the rule."""
    fake = FakeClock()
    dest = tmp_path / "p.pdf"
    adapter = FakeAdapter([throttled()] * 5)

    assert _download(adapter, dest, fake=fake, num_retries=2) is False
    assert len(adapter.sent) == 1
    assert fake.sleeps == []
    assert not dest.exists()


def test_a_read_timeout_is_still_retried_after_a_bare_429_rule_change():
    """Transport failures keep their retries — the rule is about 429 only."""
    fake = FakeClock()
    adapter = FakeAdapter([requests.exceptions.ReadTimeout("timed out"), ok()])

    _search(adapter, fake=fake)

    assert len(adapter.sent) == 2


def test_a_non_retryable_error_is_not_retried():
    adapter = FakeAdapter([(403, b"Forbidden", {}), ok()])

    with pytest.raises(arxiv.HTTPError) as excinfo:
        _search(adapter)

    assert excinfo.value.status == 403
    assert len(adapter.sent) == 1


# ---------------------------------------------------------------------------
# Transport-level failures: read timeouts and connection errors (D-TIMEOUT-01)
#
# A live /collect saw arXiv fail outright with
# "ReadTimeout: HTTPSConnectionPool(...): Read timed out. (read timeout=30.0)"
# — a transport-level failure, not an HTTP status, so it previously bypassed
# the 429/5xx retry loop entirely and failed the source on the first timeout.
# ---------------------------------------------------------------------------


def test_a_read_timeout_is_retried_and_then_succeeds():
    fake = FakeClock()
    adapter = FakeAdapter([requests.exceptions.ReadTimeout("timed out"), ok()])

    out = _search(adapter, fake=fake)

    assert out == []
    assert len(adapter.sent) == 2
    assert fake.sleeps == [10.0]  # backoff_base_seconds * 2**0, same as a 5xx


def test_a_connection_error_is_retried_and_then_succeeds():
    fake = FakeClock()
    adapter = FakeAdapter([requests.exceptions.ConnectionError("refused"), ok()])

    out = _search(adapter, fake=fake)

    assert out == []
    assert len(adapter.sent) == 2


def test_repeated_read_timeouts_are_bounded_and_reraised():
    """Exhausting retries on a timeout raises the timeout itself — there is no
    HTTP answer to fall back to and report — so /collect's per-source
    exception handling records it as this source's failure, same as any other
    exception, without ever touching Semantic Scholar or ACL Anthology."""
    fake = FakeClock()
    adapter = FakeAdapter([requests.exceptions.ReadTimeout("timed out")] * 5)

    with pytest.raises(requests.exceptions.ReadTimeout):
        _search(adapter, fake=fake, num_retries=2)

    # One retry layer only: the first try plus two retries, matching the
    # already-tested 429 exhaustion behaviour.
    assert len(adapter.sent) == 3
    assert fake.sleeps == [10.0, 20.0]


def test_a_read_timeout_still_respects_the_shared_minimum_interval():
    fake = FakeClock()
    adapter = FakeAdapter([requests.exceptions.ReadTimeout("timed out"), ok()])

    _search(adapter, fake=fake, min_interval_seconds=15.0, backoff_base_seconds=10.0)

    assert fake.sleeps == [15.0]


# ---------------------------------------------------------------------------
# PDF / full-text downloads (D-PDF-01)
#
# arXiv-hosted PDFs used to be fetched by `corpus.download_pdf`'s bare httpx
# client: no pacing, no ScholarLens User-Agent, no bounded retry, and no
# coordination with the metadata limiter — up to ten back-to-back requests to
# arxiv.org per `/collect`. They now go through the same `PoliteSession` and
# the same process-wide `LIMITER` as the API requests.
# ---------------------------------------------------------------------------

PDF_URL = "https://arxiv.org/pdf/2401.00001v1"
PDF_BYTES = b"%PDF-1.7\nfake pdf payload\n%%EOF"


def pdf_ok(body=PDF_BYTES):
    return (200, body, {})


def _download(adapter, dest, fake=None, url=PDF_URL, **kwargs):
    fake = fake or FakeClock()
    options = {
        "min_interval_seconds": 3.0,
        "num_retries": 2,
        "backoff_base_seconds": 10.0,
        "backoff_max_seconds": 60.0,
        "limiter": fake.limiter(),
        "adapter": adapter,
    } | kwargs
    return arxiv_source.download_pdf(url, str(dest), **options)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://arxiv.org/pdf/2401.00001v1", True),
        ("http://arxiv.org/pdf/2401.00001", True),
        ("https://www.arxiv.org/pdf/2401.00001", True),
        ("https://export.arxiv.org/pdf/2401.00001", True),
        ("https://ARXIV.ORG/pdf/2401.00001", True),
        ("https://arxiv.org:443/pdf/2401.00001", True),
        ("https://aclanthology.org/2022.acl-long.1.pdf", False),
        ("https://example.org/paper.pdf", False),
        ("https://notarxiv.org/pdf/1", False),
        ("https://arxiv.org.evil.example/pdf/1", False),
        ("", False),
        (None, False),
    ],
)
def test_is_arxiv_url_recognises_only_arxiv_hosts(url, expected):
    """Unpaywall resolves open-access copies on arbitrary hosts; only the arXiv
    ones get arXiv's politeness treatment, and a look-alike domain is not one."""
    assert arxiv_source.is_arxiv_url(url) is expected


def test_a_pdf_download_carries_the_scholarlens_user_agent(tmp_path):
    adapter = FakeAdapter([pdf_ok()])

    _download(adapter, tmp_path / "p.pdf", contact="ops@example.org")

    agent = adapter.sent[0].headers["User-Agent"]
    assert agent.startswith("ScholarLens/")
    assert "ops@example.org" in agent
    assert "python-httpx" not in agent


def test_a_pdf_download_omits_the_contact_when_none_is_configured(tmp_path):
    adapter = FakeAdapter([pdf_ok()])

    _download(adapter, tmp_path / "p.pdf", contact=None)

    agent = adapter.sent[0].headers["User-Agent"]
    assert agent.startswith("ScholarLens/")
    assert "contact" not in agent


def test_a_successful_download_writes_the_pdf_and_reports_success(tmp_path):
    dest = tmp_path / "nested" / "2401.00001v1.pdf"

    assert _download(FakeAdapter([pdf_ok()]), dest) is True
    assert dest.read_bytes() == PDF_BYTES


def test_a_404_fails_gracefully_without_writing_a_file(tmp_path):
    """A withdrawn or unavailable PDF is one paper's loss, not the run's."""
    dest = tmp_path / "missing.pdf"
    adapter = FakeAdapter([(404, b"Not Found", {})])

    assert _download(adapter, dest) is False
    assert not dest.exists()
    assert len(adapter.sent) == 1  # a 404 will not fix itself; not retried


def test_consecutive_pdf_downloads_are_spaced_by_the_minimum_interval(tmp_path):
    fake = FakeClock()
    limiter = fake.limiter()

    for index in range(3):
        _download(
            FakeAdapter([pdf_ok()]),
            tmp_path / f"{index}.pdf",
            fake=fake,
            limiter=limiter,
            min_interval_seconds=5.0,
        )

    assert fake.sleeps == [5.0, 5.0]


def test_metadata_and_pdf_requests_share_one_limiter(monkeypatch, tmp_path):
    """Requirement 13: a search followed by its PDF fetches must be one paced
    queue against arXiv, not two independent bursts from the same process."""
    fake = FakeClock()
    monkeypatch.setattr(arxiv_source, "LIMITER", fake.limiter())

    arxiv_source.search_topic(
        "prompt injection", min_interval_seconds=5.0, adapter=FakeAdapter([ok()])
    )
    arxiv_source.download_pdf(
        PDF_URL,
        str(tmp_path / "p.pdf"),
        min_interval_seconds=5.0,
        adapter=FakeAdapter([pdf_ok()]),
    )

    assert fake.sleeps == [5.0]


def test_a_throttled_pdf_download_is_retried_after_backoff(tmp_path):
    fake = FakeClock()
    adapter = FakeAdapter([throttled("5"), pdf_ok()])

    assert _download(adapter, tmp_path / "p.pdf", fake=fake) is True
    assert len(adapter.sent) == 2
    assert fake.sleeps == [10.0]


def test_a_pdf_download_honours_retry_after(tmp_path):
    fake = FakeClock()
    adapter = FakeAdapter([throttled("25"), pdf_ok()])

    _download(adapter, tmp_path / "p.pdf", fake=fake)

    assert fake.sleeps == [25.0]


def test_pdf_retries_are_bounded_and_the_download_fails_without_raising(tmp_path):
    """Requirement 10: arXiv still refusing after the bounded budget fails this
    one PDF; `/collect` keeps the metadata row and every other source."""
    fake = FakeClock()
    dest = tmp_path / "p.pdf"
    adapter = FakeAdapter([throttled("1")] * 5)

    assert _download(adapter, dest, fake=fake, num_retries=2) is False
    assert len(adapter.sent) == 3  # first try plus two retries, one retry layer
    assert fake.sleeps == [10.0, 20.0]
    assert not dest.exists()


def test_a_throttled_pdf_holds_back_the_next_arxiv_request(tmp_path):
    """A 429 answered to a PDF request defers the shared limiter, so the next
    caller — metadata or PDF — cannot walk straight into the same throttle."""
    fake = FakeClock()
    limiter = fake.limiter()

    _download(
        FakeAdapter([throttled("1")]),
        tmp_path / "p.pdf",
        fake=fake,
        limiter=limiter,
        num_retries=0,
        backoff_base_seconds=40.0,
    )
    limiter.acquire()

    assert fake.sleeps == [40.0]


def test_a_pdf_download_inside_a_cooldown_sends_nothing_and_fails_gracefully(tmp_path):
    """`CooldownActive` fails this PDF rather than propagating: an enrichment
    step must not abort `/collect` over one unavailable full text."""
    fake = FakeClock()
    limiter = fake.limiter()
    limiter.defer(600.0)
    adapter = FakeAdapter([pdf_ok()])

    assert (
        _download(
            adapter,
            tmp_path / "p.pdf",
            fake=fake,
            limiter=limiter,
            backoff_max_seconds=60.0,
        )
        is False
    )
    assert adapter.sent == []


def test_a_pdf_read_timeout_is_retried_and_then_succeeds(tmp_path):
    fake = FakeClock()
    dest = tmp_path / "p.pdf"
    adapter = FakeAdapter([requests.exceptions.ReadTimeout("timed out"), pdf_ok()])

    assert _download(adapter, dest, fake=fake) is True
    assert len(adapter.sent) == 2
    assert fake.sleeps == [10.0]
    assert dest.read_bytes() == PDF_BYTES


def test_repeated_pdf_timeouts_are_bounded_and_fail_without_raising(tmp_path):
    fake = FakeClock()
    adapter = FakeAdapter([requests.exceptions.ReadTimeout("timed out")] * 5)

    assert _download(adapter, tmp_path / "p.pdf", fake=fake, num_retries=2) is False
    assert len(adapter.sent) == 3


def test_a_pdf_connection_error_fails_gracefully(tmp_path):
    adapter = FakeAdapter([requests.exceptions.ConnectionError("refused")] * 5)

    assert _download(adapter, tmp_path / "p.pdf", num_retries=1) is False


def test_pdf_throttle_logs_carry_no_contact_and_no_url(caplog, tmp_path):
    contact = "ops@example.org"
    adapter = FakeAdapter([throttled(), pdf_ok()])

    with caplog.at_level(logging.WARNING):
        _download(adapter, tmp_path / "p.pdf", contact=contact)

    assert caplog.text  # the retry was reported
    assert contact not in caplog.text
    assert PDF_URL not in caplog.text


# ---------------------------------------------------------------------------
# fetch_pdf now routes through the polite downloader
# ---------------------------------------------------------------------------


def test_fetch_pdf_uses_the_polite_arxiv_downloader_by_default(monkeypatch):
    """The default must be the paced, identified path — never the unpaced
    `corpus.download_pdf` this fix moved arXiv traffic off."""
    calls = {}

    def fake_download_pdf(url, dest_path, **kwargs):
        calls["url"] = url
        calls["dest_path"] = dest_path
        return True

    monkeypatch.setattr(arxiv_source, "download_pdf", fake_download_pdf)
    monkeypatch.setattr(
        "app.ingestion.corpus.download_pdf",
        lambda *a, **k: pytest.fail("arXiv PDFs must not use the unpaced downloader"),
    )

    dest = arxiv_source.fetch_pdf(
        {"arxiv_id": "1706.03762", "pdf_url": PDF_URL}, "/tmp/pdfs"
    )

    assert calls["url"] == PDF_URL
    assert calls["dest_path"] == "/tmp/pdfs/1706.03762.pdf"
    assert dest == "/tmp/pdfs/1706.03762.pdf"


def test_fetch_pdf_accepts_a_configured_downloader(monkeypatch):
    """`/collect` injects one downloader carrying the run's Settings, so a
    configured contact and interval reach the PDF path too."""
    seen = []

    dest = arxiv_source.fetch_pdf(
        {"arxiv_id": "1706.03762", "pdf_url": PDF_URL},
        "/tmp/pdfs",
        download=lambda url, dest_path: seen.append((url, dest_path)) or True,
    )

    assert seen == [(PDF_URL, "/tmp/pdfs/1706.03762.pdf")]
    assert dest == "/tmp/pdfs/1706.03762.pdf"


# ---------------------------------------------------------------------------
# Facet synonyms (S1 audit, 2026-10-10)
#
# The relevance gate knows that "cross-lingual" and "code-mixed" evidence the
# same facet as "multilingual", but the arXiv query still demanded the literal
# word, so such papers never became candidates. `/collect` now passes the
# gate's query groups: each group is one facet, its alternatives are OR'd, and
# the groups are still AND'd -- every facet still constrains the search.
# ---------------------------------------------------------------------------


def test_query_groups_or_a_facets_synonyms_and_and_the_facets():
    adapter = FakeAdapter([ok()])

    _search(
        adapter,
        topic="Multilingual prompt injection",
        query_groups=[["multilingual", "cross-lingual"], ["prompt"], ["injection"]],
    )

    assert _sent_query(adapter) == (
        '(all:"multilingual" OR all:"cross-lingual") AND all:"prompt" AND all:"injection"'
    )


def test_without_query_groups_the_topic_query_is_unchanged():
    adapter = FakeAdapter([ok()])

    _search(adapter, topic="Multilingual prompt injection", query_groups=None)

    assert _sent_query(adapter) == (
        'all:"Multilingual" AND all:"prompt" AND all:"injection"'
    )


def test_query_group_terms_lose_double_quotes():
    adapter = FakeAdapter([ok()])

    _search(adapter, query_groups=[['say "hi"']])

    assert _sent_query(adapter) == 'all:"say hi"'


# ---------------------------------------------------------------------------
# A 200 is not a PDF (D-FULLTEXT-01): the arXiv path validates like every other
# ---------------------------------------------------------------------------


def _download_result(adapter, dest, **kwargs):
    options = {
        "min_interval_seconds": 3.0,
        "num_retries": 0,
        "limiter": FakeClock().limiter(),
        "adapter": adapter,
    } | kwargs
    return arxiv_source.download_pdf_result(PDF_URL, str(dest), **options)


def test_an_html_answer_is_not_stored_as_a_pdf(tmp_path):
    dest = tmp_path / "p.pdf"
    adapter = FakeAdapter(
        [(200, b"<html>captcha</html>", {"Content-Type": "text/html; charset=utf-8"})]
    )

    result = _download_result(adapter, dest)

    assert result.status == "not_pdf"
    assert not dest.exists()


def test_a_body_without_the_pdf_signature_is_rejected(tmp_path):
    dest = tmp_path / "p.pdf"
    adapter = FakeAdapter(
        [(200, b"not a pdf at all", {"Content-Type": "application/pdf"})]
    )

    result = _download_result(adapter, dest)

    assert result.status == "file_validation_failed"
    assert not dest.exists()


def test_an_oversized_pdf_is_not_stored(tmp_path):
    dest = tmp_path / "p.pdf"

    result = _download_result(FakeAdapter([pdf_ok()]), dest, max_bytes=8)

    assert result.status == "too_large"
    assert not dest.exists()
    assert not list(tmp_path.iterdir())  # no partial file left behind


def test_a_failed_download_never_replaces_a_stored_pdf(tmp_path):
    dest = tmp_path / "p.pdf"
    dest.write_bytes(PDF_BYTES)
    adapter = FakeAdapter([(200, b"<html></html>", {"Content-Type": "text/html"})])

    _download_result(adapter, dest)

    assert dest.read_bytes() == PDF_BYTES


def test_a_throttled_pdf_is_reported_as_rate_limited(tmp_path):
    result = _download_result(FakeAdapter([throttled()]), tmp_path / "p.pdf")

    assert result.status == "rate_limited"
    assert result.retryable


def test_the_boolean_downloader_keeps_its_contract(tmp_path):
    assert _download(FakeAdapter([pdf_ok()]), tmp_path / "a.pdf") is True
    html = (200, b"<html></html>", {"Content-Type": "text/html"})
    assert _download(FakeAdapter([html]), tmp_path / "b.pdf") is False
