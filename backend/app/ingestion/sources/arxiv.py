"""arXiv ingestion source (INGEST-03).

Queries the arXiv API via the official `arxiv` PyPI package and maps results
onto the corpus-shaped dict contract from `app.ingestion.corpus`.

D-13 (corrected post-research): the installed `arxiv` v4.0.1 package has no
`download_pdf`/`download_source` helper on `Result` (removed in a prior major
version), so PDFs are fetched against `Result.pdf_url` — never via a
package-provided download method.

Politeness (arXiv API Terms of Use): every arXiv request this process makes —
metadata *and* PDF — goes through `PoliteSession`, which waits its turn on the
process-wide `LIMITER`, identifies ScholarLens in the User-Agent, and owns the
one bounded retry loop for 429/5xx answers. The library's own retry and delay
layers are switched off, so a 429 is never retried twice and pacing has a
single source of truth.

D-PDF-01: PDFs used to bypass all of that. `corpus.download_pdf`'s bare httpx
client sent no ScholarLens User-Agent, took no turn on the limiter, and had no
backoff — so one `/collect` put a single paced metadata request to
export.arxiv.org and then up to ten unpaced requests to arxiv.org, plus any
arXiv-hosted copy Unpaywall resolved afterwards. `download_pdf` below moves
that traffic onto the same session and the same limiter as the API requests,
so the two can no longer form independent bursts. `corpus.download_pdf` stays
as it is for non-arXiv open-access hosts.

D-TIMEOUT-01: a slow arXiv response (`requests.exceptions.Timeout`, which
covers `ReadTimeout`/`ConnectTimeout`) or a connection-level failure
(`requests.exceptions.ConnectionError`) is a transport-level failure, not an
HTTP status — `send_with_retries`'s status-code check never sees it, so it
used to propagate on the first occurrence with no retry at all. `PoliteSession`
now opts in to retrying those two exception types through the same bounded
retry budget (`num_retries`/backoff) a 429/5xx already gets, so a transient
timeout is retried once before failing the source for the run. The 30s
per-request timeout is unchanged — a source that is genuinely down should fail
and be reported, not be waited on longer.
"""

import logging
import re
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import urlparse

import arxiv
import requests

from app.ingestion.pdf_store import DownloadResult
from app.ingestion.pdf_store import store as store_pdf
from app.ingestion.rate_limit import CooldownActive, RateLimiter, send_with_retries
from app.ingestion.retrieval import Candidates, error_category
from app.ingestion.retrieval import report as retrieval_report
from app.ingestion.topic import meaningful_terms

logger = logging.getLogger(__name__)

APP_NAME = "ScholarLens"

# One limiter for every arXiv request this process makes — metadata and PDF
# alike, so the two cannot burst independently. Each call sets its interval
# from Settings; this default only applies until then.
LIMITER = RateLimiter(min_interval_seconds=5.0)

# The library sends no timeout, so one stalled response would otherwise hold a
# /collect run open indefinitely.
REQUEST_TIMEOUT_SECONDS = 30.0

# Hosts whose traffic owes arXiv's politeness rules. `arxiv.org` serves the
# PDFs, `export.arxiv.org` the API; both are arXiv's own infrastructure.
ARXIV_HOSTS = frozenset({"arxiv.org", "www.arxiv.org", "export.arxiv.org"})

_PDF_CHUNK_SIZE = 1024 * 1024


def _app_version() -> str:
    try:
        return version("scholarlens-backend")
    except PackageNotFoundError:
        return "0.0.0+unknown"


def build_user_agent(contact: str | None) -> str:
    """Identify ScholarLens to arXiv, with the operator's contact when one is set."""
    purpose = "automatic research-gap identification"
    if contact:
        purpose = f"{purpose}; contact: {contact}"
    return f"{APP_NAME}/{_app_version()} ({purpose})"


def _all_fields_query(topic: str) -> str:
    """The topic as an all-fields AND of its terms (D-QUERY-02).

    Sent unprefixed, `search_query=prompt injection` leaves both the field and
    the word grouping to arXiv's parser, so the topic is always prefixed
    `all:` — every field, no operator invented on the user's behalf.

    D-QUERY-01 wrapped the whole topic in one phrase, `all:"<topic>"`, which
    requires those words adjacent and in order inside a single field. That is
    narrower than a topic search: `all:"Multilingual prompt injection"` matched
    nothing while Semantic Scholar, which tokenizes the query itself, returned
    ten papers for the same topic. The topic is now split by
    `app.ingestion.topic.meaningful_terms` and each term sent as its own
    all-fields phrase, joined with AND. A paper must still carry *every*
    meaningful term the user typed — this is not broadened to OR, which would
    return papers about prompting in general — but the terms no longer have to
    sit next to each other. A single-term topic produces exactly the query
    D-QUERY-01 produced. This is a search-relevance fix; it has no bearing on
    rate limiting.
    """
    terms = meaningful_terms(topic) or [topic.replace('"', "").strip()]
    return " AND ".join(f'all:"{term}"' for term in terms)


def _grouped_query(query_groups: Sequence[Sequence[str]]) -> str:
    """Facet groups as an AND of ORs (S1 audit, 2026-10-10).

    Each group is one facet of the topic and its alternatives are synonyms the
    relevance gate validated for retrieval ("multilingual", "cross-lingual",
    ...), so they are OR'd; the groups are AND'd, so every facet still
    constrains the search exactly as D-QUERY-02 requires. Double quotes are
    dropped from each term for the reason `meaningful_terms` gives.
    """

    def clause(group: Sequence[str]) -> str:
        terms = [
            " ".join(term.replace('"', "").split())
            for term in group
            if term.replace('"', "").strip()
        ]
        phrases = [f'all:"{term}"' for term in dict.fromkeys(terms)]
        return phrases[0] if len(phrases) == 1 else f"({' OR '.join(phrases)})"

    return " AND ".join(clause(group) for group in query_groups if group)


def is_arxiv_url(url: str | None) -> bool:
    """Whether `url` is served by arXiv, and so owes arXiv's politeness rules.

    Unpaywall resolves open-access copies on arbitrary publisher hosts; only
    the arXiv-hosted ones are paced by this module's limiter. Matching is on
    the parsed hostname, never a substring, so `arxiv.org.example` is not arXiv.
    """
    if not url:
        return False
    try:
        host = urlparse(url).hostname
    except ValueError:
        return False  # a malformed URL from a third party is simply not arXiv
    return host in ARXIV_HOSTS


class PoliteSession(requests.Session):
    """The `requests.Session` every arXiv request goes through — API and PDF.

    arxiv 4.x hardcodes its generic `arxiv.py/<version>` user-agent on each
    request and offers no hook for pacing or Retry-After (its `HTTPError` keeps
    only the status code), so the session is the one place all three can be
    applied to every request, pagination included. `download_pdf` reuses it so
    full-text traffic gets the same treatment rather than a second, competing
    implementation.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        limiter: RateLimiter,
        max_retries: int,
        backoff_base_seconds: float,
        backoff_max_seconds: float,
        source: str = "arXiv",
    ) -> None:
        super().__init__()
        self._user_agent = user_agent
        self._limiter = limiter
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._source = source

    def request(self, method, url, **kwargs):
        headers = {
            key: value
            for key, value in (kwargs.pop("headers", None) or {}).items()
            if key.lower() != "user-agent"
        }
        headers["User-Agent"] = self._user_agent
        kwargs["headers"] = headers
        kwargs.setdefault("timeout", REQUEST_TIMEOUT_SECONDS)
        send = super().request
        return send_with_retries(
            lambda: send(method, url, **kwargs),
            limiter=self._limiter,
            source=self._source,
            max_retries=self._max_retries,
            backoff_base_seconds=self._backoff_base_seconds,
            backoff_max_seconds=self._backoff_max_seconds,
            # A read timeout or a connection failure is transient exactly like
            # a 5xx (D-TIMEOUT-01) — retried through the same bounded budget,
            # not left to propagate on the first occurrence.
            retryable_exceptions=(
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
            ),
            # D-429-03: arXiv answers `429 Rate exceeded.` with no Retry-After
            # and no rate-limit headers, from a block that outlives any backoff
            # we could compute. Retrying it only sends more requests to a host
            # already refusing them, so one attempt is made and the source is
            # parked for the run; /collect reports it failed and carries on.
            retry_429_without_retry_after=False,
        )


def _polite_session(
    *,
    contact: str | None,
    limiter: RateLimiter,
    num_retries: int,
    backoff_base_seconds: float,
    backoff_max_seconds: float,
    adapter: requests.adapters.BaseAdapter | None,
    source: str,
) -> PoliteSession:
    """One session builder, so metadata and PDF requests cannot drift apart."""
    session = PoliteSession(
        user_agent=build_user_agent(contact),
        limiter=limiter,
        max_retries=num_retries,
        backoff_base_seconds=backoff_base_seconds,
        backoff_max_seconds=backoff_max_seconds,
        source=source,
    )
    if adapter is not None:
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    return session


def _candidate(r) -> dict:
    return {
        "title": r.title,
        "authors": [a.name for a in r.authors],
        "year": r.published.year,
        "venue": None,
        "doi": r.doi,
        "abstract": r.summary,
        "arxiv_id": r.get_short_id(),
        "pdf_url": r.pdf_url,
        "source_url": getattr(r, "entry_id", None),
        "source": "arxiv",
    }


_VERSION = re.compile(r"v\d+$")


def _versionless(arxiv_id: str | None) -> str | None:
    return _VERSION.sub("", arxiv_id) if arxiv_id else arxiv_id


def search_topic(
    topic: str,
    max_results: int = 10,
    num_retries: int = 2,
    min_interval_seconds: float = 5.0,
    backoff_base_seconds: float = 10.0,
    backoff_max_seconds: float = 60.0,
    contact: str | None = None,
    limiter: RateLimiter | None = None,
    adapter: requests.adapters.BaseAdapter | None = None,
    query_groups: Sequence[Sequence[str]] | None = None,
    page_size: int | None = None,
) -> Candidates:
    """Search arXiv for `topic` and return corpus-shaped dicts, with a report.

    With `query_groups` (the relevance gate's facets and their validated
    synonyms) the query is built from those groups; without them, from the
    topic's terms as before.

    **Paging (D-S1-RETRIEVAL-01).** `max_results` is the retrieval ceiling,
    `page_size` (default: the ceiling, so one request) the results per
    request. Pages are read in arXiv's relevance order until arXiv's reported
    total is exhausted or the ceiling is reached; every request waits its turn
    on the shared limiter. Results are de-duplicated across pages by
    version-less arXiv id, keeping the first (best-ranked) copy. The returned
    `Candidates.retrieval` says how far the read got: stopping at the ceiling,
    on an empty page before the reported total, or on a failed later page is
    reported incomplete with the candidates read so far. A failure on the
    *first* page still raises, as before: there is nothing to return.

    Raises `arxiv.HTTPError` when arXiv still refuses after `num_retries`
    retries, and `CooldownActive` while an earlier refusal's cooldown runs;
    `/collect` records either as this source's failure. `limiter` and `adapter`
    exist for tests; callers use the process-wide `LIMITER` and the network.
    """
    limiter = LIMITER if limiter is None else limiter
    limiter.set_min_interval(min_interval_seconds)
    session = _polite_session(
        contact=contact,
        limiter=limiter,
        num_retries=num_retries,
        backoff_base_seconds=backoff_base_seconds,
        backoff_max_seconds=backoff_max_seconds,
        adapter=adapter,
        source="arXiv",
    )
    page_size = min(page_size or max_results, max_results)

    # The library's retries and delay are off: PoliteSession owns both. Its
    # feed parser is used page by page so the reported total is visible.
    client = arxiv.Client(page_size=page_size, delay_seconds=0.0, num_retries=0)
    client._session = session  # arxiv 4.x: the one Session every API request uses
    query = _grouped_query(query_groups) if query_groups else _all_fields_query(topic)
    # The query as sent, so a run that returns nothing can be told apart from a
    # run that was never asked the right question (D-QUERY-02).
    logger.info(
        "arXiv search_query=%s ceiling=%d page_size=%d", query, max_results, page_size
    )
    search = arxiv.Search(query=query, max_results=max_results)

    candidates: list[dict] = []
    seen: set[str] = set()
    duplicates = 0
    total: int | None = None
    offset = pages_requested = pages_succeeded = fetched = 0
    stop_reason = "exhausted"
    error: str | None = None
    try:
        while offset < max_results:
            size = min(page_size, max_results - offset)
            url = client._format_url(search, offset, size)
            pages_requested += 1
            try:
                feed = client._parse_feed(url, first_page=offset == 0)
            except arxiv.UnexpectedEmptyPageError:
                # arXiv sometimes answers a later page with nothing before its
                # own total is reached; what was read is kept, as incomplete.
                stop_reason = "empty_page"
                break
            except Exception as exc:
                if not pages_succeeded:
                    raise
                stop_reason, error = "page_error", error_category(exc)
                logger.warning(
                    "arXiv page at offset %d failed (%s); keeping %d candidates",
                    offset,
                    error,
                    len(candidates),
                )
                break
            if feed.malformed:
                if not pages_succeeded:
                    raise ValueError("arXiv returned a malformed feed")
                stop_reason, error = "page_error", "malformed_feed"
                break
            pages_succeeded += 1
            if total is None:
                total = feed.header.total_results
            fetched += len(feed.results)
            for result in feed.results:
                key = _versionless(result.get_short_id())
                if key in seen:
                    duplicates += 1
                    continue
                seen.add(key)
                candidates.append(_candidate(result))
            offset += len(feed.results)
            if not feed.results or offset >= (total or 0):
                break
        else:
            if total is not None and offset < total:
                stop_reason = "ceiling"
    finally:
        session.close()
    return Candidates(
        candidates,
        retrieval_report(
            query=query,
            params={"ceiling": max_results, "page_size": page_size},
            total_reported=total,
            pages_requested=pages_requested,
            pages_succeeded=pages_succeeded,
            records_fetched=fetched,
            records_dropped={"duplicate_across_pages": duplicates},
            candidates=len(candidates),
            stop_reason=stop_reason,
            error=error,
        ),
    )


def download_pdf_result(
    url: str,
    dest_path: str,
    *,
    contact: str | None = None,
    num_retries: int = 2,
    min_interval_seconds: float = 5.0,
    backoff_base_seconds: float = 10.0,
    backoff_max_seconds: float = 60.0,
    limiter: RateLimiter | None = None,
    adapter: requests.adapters.BaseAdapter | None = None,
    max_bytes: int = 50 * 1024 * 1024,
) -> DownloadResult:
    """Fetch one arXiv-hosted PDF under the same politeness rules as the API.

    The request waits its turn on the same process-wide `LIMITER` the metadata
    requests use, carries the ScholarLens User-Agent, and gets the same bounded
    Retry-After/backoff budget — so ten PDFs are ten paced requests, not a
    burst, and a 429 answered here holds back the *next* arXiv request of
    either kind.

    The body is stored only when it is a PDF within `max_bytes`
    (`app.ingestion.pdf_store`, D-FULLTEXT-01); a file already at `dest_path`
    survives a failed download. Nothing is raised — one paper's missing full
    text must never abort a `/collect` run — and the result says what
    happened: a 404, a 429 that outlived the retry budget, a cooldown still
    running from an earlier refusal, a transport error, or a non-PDF body.
    """
    limiter = LIMITER if limiter is None else limiter
    limiter.set_min_interval(min_interval_seconds)
    session = _polite_session(
        contact=contact,
        limiter=limiter,
        num_retries=num_retries,
        backoff_base_seconds=backoff_base_seconds,
        backoff_max_seconds=backoff_max_seconds,
        adapter=adapter,
        source="arXiv PDF",
    )
    # Logs name the source and the status only — never the URL, which for an
    # Unpaywall-resolved location can carry a query string.
    try:
        try:
            response = session.get(url, stream=True)
        except CooldownActive as exc:
            logger.warning(
                "arXiv PDF not fetched: cooldown active, %.0fs remaining",
                exc.wait_seconds,
            )
            return DownloadResult("rate_limited", detail="cooldown")
        except requests.exceptions.Timeout:
            logger.warning("arXiv PDF not fetched: Timeout")
            return DownloadResult("timeout")
        except requests.exceptions.RequestException as exc:
            logger.warning("arXiv PDF not fetched: %s", type(exc).__name__)
            return DownloadResult("network_error", detail=type(exc).__name__)
        with response:
            if response.status_code != requests.codes.OK:
                logger.warning(
                    "arXiv PDF not fetched: answered HTTP %d", response.status_code
                )
                status = "rate_limited" if response.status_code == 429 else "http_error"
                return DownloadResult(status, response.status_code)
            try:
                result = store_pdf(
                    response.iter_content(_PDF_CHUNK_SIZE),
                    dest_path,
                    max_bytes=max_bytes,
                    content_type=response.headers.get("Content-Type"),
                )
            except requests.exceptions.RequestException as exc:
                logger.warning("arXiv PDF not stored: %s", type(exc).__name__)
                return DownloadResult("network_error", 200, detail=type(exc).__name__)
            if not result.ok:
                logger.warning("arXiv PDF not stored: %s", result.status)
            return result
    finally:
        session.close()


def download_pdf(url: str, dest_path: str, **kwargs) -> bool:
    """`download_pdf_result`, as True when a validated PDF was stored."""
    return download_pdf_result(url, dest_path, **kwargs).ok


def fetch_pdf(candidate: dict, dest_dir: str, download=None) -> str | None:
    """Fetch `candidate`'s PDF politely; return its path, or None on failure.

    `download` lets `/collect` inject a downloader carrying that run's Settings
    (contact, interval, retry budget). It defaults to this module's own
    `download_pdf` so no call site can silently fall back to an unpaced fetch.
    """
    dest_path = f"{dest_dir}/{candidate['arxiv_id']}.pdf"
    fetch = download_pdf if download is None else download
    return dest_path if fetch(candidate["pdf_url"], dest_path) else None
