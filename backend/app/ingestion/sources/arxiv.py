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
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlparse

import arxiv
import requests

from app.ingestion.rate_limit import CooldownActive, RateLimiter, send_with_retries
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
) -> list[dict]:
    """Search arXiv for `topic` and return a list of corpus-shaped dicts.

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

    # `page_size`, not `max_results`, is what the library puts in the request
    # URL (default 100), so the page is sized to what is kept. Its retries and
    # delay are off: PoliteSession owns both.
    client = arxiv.Client(page_size=max_results, delay_seconds=0.0, num_retries=0)
    client._session = session  # arxiv 4.x: the one Session every API request uses
    query = _all_fields_query(topic)
    # The query as sent, so a run that returns nothing can be told apart from a
    # run that was never asked the right question (D-QUERY-02).
    logger.info("arXiv search_query=%s max_results=%d", query, max_results)
    search = arxiv.Search(query=query, max_results=max_results)

    candidates = []
    try:
        for r in client.results(search):
            candidates.append(
                {
                    "title": r.title,
                    "authors": [a.name for a in r.authors],
                    "year": r.published.year,
                    "venue": None,
                    "doi": r.doi,
                    "abstract": r.summary,
                    "arxiv_id": r.get_short_id(),
                    "pdf_url": r.pdf_url,
                    "source": "arxiv",
                }
            )
    finally:
        session.close()
    return candidates


def download_pdf(
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
) -> bool:
    """Fetch one arXiv-hosted PDF under the same politeness rules as the API.

    The request waits its turn on the same process-wide `LIMITER` the metadata
    requests use, carries the ScholarLens User-Agent, and gets the same bounded
    Retry-After/backoff budget — so ten PDFs are ten paced requests, not a
    burst, and a 429 answered here holds back the *next* arXiv request of
    either kind.

    Returns True when the file was written and False on every failure: a 404,
    a 429 that outlived the retry budget, a cooldown still running from an
    earlier refusal, or a transport error. Nothing is raised — one paper's
    missing full text must never abort a `/collect` run.
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
            return False
        except requests.exceptions.RequestException as exc:
            logger.warning("arXiv PDF not fetched: %s", type(exc).__name__)
            return False
        with response:
            if response.status_code != requests.codes.OK:
                logger.warning(
                    "arXiv PDF not fetched: answered HTTP %d", response.status_code
                )
                return False
            try:
                Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
                with open(dest_path, "wb") as handle:
                    handle.writelines(response.iter_content(_PDF_CHUNK_SIZE))
            except (OSError, requests.exceptions.RequestException) as exc:
                logger.warning("arXiv PDF not stored: %s", type(exc).__name__)
                return False
        return True
    finally:
        session.close()


def fetch_pdf(candidate: dict, dest_dir: str, download=None) -> str | None:
    """Fetch `candidate`'s PDF politely; return its path, or None on failure.

    `download` lets `/collect` inject a downloader carrying that run's Settings
    (contact, interval, retry budget). It defaults to this module's own
    `download_pdf` so no call site can silently fall back to an unpaced fetch.
    """
    dest_path = f"{dest_dir}/{candidate['arxiv_id']}.pdf"
    fetch = download_pdf if download is None else download
    return dest_path if fetch(candidate["pdf_url"], dest_path) else None
