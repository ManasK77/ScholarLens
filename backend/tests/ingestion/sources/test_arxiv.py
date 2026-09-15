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


class RecordingClient:
    """Stand-in for arxiv.Client that records how it was configured."""

    last: ClassVar["RecordingClient | None"] = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.search_max_results = None
        RecordingClient.last = self

    def results(self, search):
        self.search_max_results = search.max_results
        return iter(())


def test_search_topic_sizes_the_request_page_to_max_results(monkeypatch):
    """arxiv.Client puts `page_size` (library default 100), not `max_results`,
    into the request URL — so an unsized client asks arXiv for 100 entries
    to keep 10."""
    monkeypatch.setattr(arxiv_source.arxiv, "Client", RecordingClient)

    arxiv_source.search_topic("anything", max_results=7)

    assert RecordingClient.last.kwargs["page_size"] == 7
    assert RecordingClient.last.search_max_results == 7


def test_search_topic_defaults_to_ten_results(monkeypatch):
    monkeypatch.setattr(arxiv_source.arxiv, "Client", RecordingClient)

    arxiv_source.search_topic("anything")

    assert RecordingClient.last.kwargs["page_size"] == 10


def test_the_library_retry_and_delay_layers_are_switched_off(monkeypatch):
    """ScholarLens owns spacing and retries. A second retry layer inside the
    library would retry the same 429 again, and its delay resets with every
    new Client, so it paces nothing across calls."""
    monkeypatch.setattr(arxiv_source.arxiv, "Client", RecordingClient)

    arxiv_source.search_topic("anything", num_retries=3)

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

    bare = BareResult()

    def fake_results(self, search):
        yield bare

    monkeypatch.setattr(arxiv_source.arxiv.Client, "results", fake_results)

    out = arxiv_source.search_topic("anything")

    assert len(out) == 1
    assert out[0]["arxiv_id"] == "9999.99999"


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
    assert query["search_query"] == ['all:"prompt injection"']


# ---------------------------------------------------------------------------
# Query construction (D-QUERY-01)
#
# An unprefixed `search_query=prompt injection` leaves the field and the
# word-grouping to arXiv's parser. ScholarLens searches *broadly for the topic
# the user typed*, so the topic is sent as an explicit all-fields phrase:
# `all:"prompt injection"`. This is a search-relevance fix and has nothing to
# do with rate limiting.
# ---------------------------------------------------------------------------


def _sent_query(adapter) -> str:
    return parse_qs(urlparse(adapter.sent[0].url).query)["search_query"][0]


def test_the_topic_is_sent_as_an_explicit_all_fields_phrase():
    adapter = FakeAdapter([ok()])

    _search(adapter)

    assert _sent_query(adapter) == 'all:"prompt injection"'


def test_a_single_word_topic_is_also_sent_as_an_all_fields_query():
    adapter = FakeAdapter([ok()])

    _search(adapter, topic="transformers")

    assert _sent_query(adapter) == 'all:"transformers"'


def test_the_url_is_encoded_rather_than_carrying_raw_spaces_or_quotes():
    """`search_query` is percent-encoded, so the topic survives the round trip."""
    adapter = FakeAdapter([ok()])

    _search(adapter)

    assert " " not in adapter.sent[0].url
    assert '"' not in adapter.sent[0].url


@pytest.mark.parametrize(
    "topic",
    [
        "retrieval-augmented generation",
        "GPT-4 (vision)",
        "hate speech: a survey",
        "back-translation & paraphrase",
        "Müller's method",
    ],
)
def test_punctuation_in_a_topic_survives_the_round_trip(topic):
    """Spaces, hyphens, parentheses, colons, ampersands and non-ASCII are all
    carried through as the user typed them — no operator is invented."""
    adapter = FakeAdapter([ok()])

    _search(adapter, topic=topic)

    assert _sent_query(adapter) == f'all:"{topic}"'


def test_double_quotes_inside_a_topic_cannot_break_out_of_the_phrase():
    """arXiv's grammar has no documented escape for a quote inside a quoted
    phrase, so embedded quotes are dropped rather than sent as-is, where they
    would terminate the phrase early and change the query's meaning."""
    adapter = FakeAdapter([ok()])

    _search(adapter, topic='the "alignment tax"')

    assert _sent_query(adapter) == 'all:"the alignment tax"'


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
