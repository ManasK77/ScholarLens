"""S1 full-text acquisition with a per-paper audit trail (D-FULLTEXT-01).

The 2026-10-12 audit found half the corpus metadata-only with no way to tell
why: Unpaywall's outcomes were counted but not tied to papers, Semantic
Scholar's open-access link was dropped on the way into `papers`, and an
arXiv identifier carried by a Semantic Scholar record was never used. This
module is the one place a corpus paper without full text gets one, and
every provider it tries leaves a `fulltext_attempts` row saying what
happened.

**Providers, in order, first success wins** (per paper lacking full text):

1. ``semantic_scholar_oa`` -- the record's `openAccessPdf` URL, when given;
2. ``arxiv`` -- ``https://arxiv.org/pdf/<arxiv_id>`` when the paper has one;
3. ``acl_anthology`` -- ``https://aclanthology.org/<acl_id>.pdf``;
4. ``unpaywall`` -- a DOI lookup (needs ``UNPAYWALL_EMAIL``), then the
   location it names.

A URL already tried for the paper is not fetched again by a later provider.
arXiv-hosted URLs go through arXiv's polite downloader (shared limiter,
ScholarLens User-Agent); everything else through `safe_download`.

**Safety.** `safe_download` refuses non-HTTP(S) URLs, URLs with
credentials, non-standard ports, and hosts that resolve to anything but
public addresses (loopback, private, link-local, reserved, multicast) --
checked again for every redirect, which it follows itself (at most five).
A body is stored only through `app.ingestion.pdf_store`: a PDF signature,
under the size limit, never HTML. Requests time out; a timeout, 429 or 5xx
is retried a bounded number of times. Name resolution and connection are
separate steps, so a host whose DNS answer changes between them (DNS
rebinding) is not fully excluded; the address check stops every
misconfiguration and ordinary SSRF attempt, not an adversarial resolver.

**Audit.** Each attempt records the provider, the stage (``discovery`` for a
source's own PDF fetched while admitting it, ``enrichment`` for this step),
the outcome, the HTTP status, whether retrying could help, and the URL's
host and path (never its query string, which can carry tokens). Rows are
appended, never rewritten, so an earlier success stays on record.

Outcomes: the `pdf_store.DOWNLOAD_STATUSES`, plus ``existing_full_text_reused``
(a merge onto a paper that already had full text), ``no_doi``,
``not_configured``, ``doi_not_found``, ``no_oa_location``,
``lookup_http_error``, ``lookup_timeout``, ``lookup_network_error``,
``lookup_invalid_response`` (Unpaywall lookups), ``download_failed`` (a
discovery fetch whose cause the downloader did not report) and
``not_attempted`` (the step's time budget ran out first).
"""

import ipaddress
import logging
import socket
import time
from collections import Counter
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingestion import corpus, pdf_store, unpaywall
from app.ingestion.collection_audit import FulltextAttempt
from app.ingestion.pdf_store import DownloadResult
from app.ingestion.sources import arxiv

logger = logging.getLogger(__name__)

PROVIDERS = ("semantic_scholar_oa", "arxiv", "acl_anthology", "unpaywall")
_LOOKUP_OUTCOME = {
    "no_oa_copy": "no_oa_location",
    "doi_not_found": "doi_not_found",
    "http_error": "lookup_http_error",
    "timeout": "lookup_timeout",
    "network_error": "lookup_network_error",
    "invalid_response": "lookup_invalid_response",
}
_RETRYABLE_LOOKUPS = {"lookup_timeout", "lookup_network_error", "lookup_http_error"}
_REDIRECTS = (301, 302, 303, 307, 308)
_MAX_REDIRECTS = 5


# ---------------------------------------------------------------------------
# URL safety and the generic downloader
# ---------------------------------------------------------------------------


def resolve_host(host: str) -> list[str]:
    """Every address `host` resolves to (patched in tests)."""
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


def unsafe_url_reason(url: str) -> str | None:
    """Why `url` must not be fetched, or None when it may be."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "malformed_url"
    if parts.scheme not in ("http", "https"):
        return "scheme"
    if not parts.hostname:
        return "no_host"
    if parts.username or parts.password:
        return "credentials_in_url"
    if port not in (None, 80, 443):
        return "port"
    try:
        addresses = [ipaddress.ip_address(parts.hostname)]
    except ValueError:
        try:
            addresses = [
                ipaddress.ip_address(a.split("%")[0])
                for a in resolve_host(parts.hostname)
            ]
        except OSError:
            return "unresolvable_host"
    if not addresses or not all(a.is_global and not a.is_multicast for a in addresses):
        return "non_public_address"
    return None


def safe_location(url: str | None) -> str | None:
    """Host and path of `url` -- never its query string or fragment."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    return f"{parts.hostname or ''}{parts.path}"[:300] or None


def safe_download(
    url: str,
    dest_path: str,
    *,
    timeout: float = 30.0,
    max_bytes: int = 50 * 1024 * 1024,
    max_retries: int = 0,
    retry_wait_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> DownloadResult:
    """Fetch `url` into `dest_path` if it is a public-host PDF. Never raises."""
    result = DownloadResult("network_error")
    for attempt in range(max_retries + 1):
        if attempt:
            sleep(retry_wait_seconds * attempt)
        result = _download_once(url, dest_path, timeout=timeout, max_bytes=max_bytes)
        if not result.retryable:
            break
    return result


def _download_once(url: str, dest_path: str, *, timeout: float, max_bytes: int):
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        reason = unsafe_url_reason(current)
        if reason:
            return DownloadResult("blocked_url", detail=reason)
        try:
            with httpx.stream(
                "GET", current, timeout=timeout, follow_redirects=False
            ) as resp:
                status = resp.status_code
                headers = getattr(resp, "headers", None) or {}
                if status in _REDIRECTS and headers.get("location"):
                    current = urljoin(current, headers["location"])
                    continue
                if status != 200:
                    kind = "rate_limited" if status == 429 else "http_error"
                    return DownloadResult(kind, status)
                return pdf_store.store(
                    resp.iter_bytes(),
                    dest_path,
                    max_bytes=max_bytes,
                    content_type=headers.get("content-type"),
                    http_status=status,
                )
        except httpx.TimeoutException:
            return DownloadResult("timeout")
        except httpx.HTTPError as exc:
            # The class name only: a message can carry the URL.
            return DownloadResult("network_error", detail=type(exc).__name__)
    return DownloadResult("blocked_url", detail="too_many_redirects")


# ---------------------------------------------------------------------------
# The audit trail
# ---------------------------------------------------------------------------


class AttemptLog:
    """Appends `fulltext_attempts` rows for one collection run."""

    def __init__(self, session: Session, run_id: int | None) -> None:
        self.session = session
        self.run_id = run_id
        self.outcomes: Counter[tuple[str, str]] = Counter()

    def record(
        self,
        paper_id: int,
        provider: str,
        stage: str,
        outcome: str,
        *,
        result: DownloadResult | None = None,
        url: str | None = None,
        location_found: bool | None = None,
        retryable: bool | None = None,
        pdf_path: str | None = None,
        detail: str | None = None,
    ) -> FulltextAttempt:
        row = FulltextAttempt(
            run_id=self.run_id,
            paper_id=paper_id,
            provider=provider,
            stage=stage,
            outcome=outcome,
            http_status=result.http_status if result else None,
            retryable=result.retryable if result and retryable is None else retryable,
            location_found=location_found,
            location=safe_location(url),
            pdf_path=pdf_path,
            detail=(detail or (result.detail if result else None)),
        )
        self.session.add(row)
        self.session.flush()
        self.outcomes[(provider, outcome)] += 1
        logger.info(
            "fulltext paper=%d provider=%s stage=%s outcome=%s",
            paper_id,
            provider,
            stage,
            outcome,
        )
        return row

    def summary(self) -> dict:
        by_provider: dict[str, dict[str, int]] = {}
        for (provider, outcome), n in sorted(self.outcomes.items()):
            by_provider.setdefault(provider, {})[outcome] = n
        return {
            "attempts": sum(self.outcomes.values()),
            "by_provider": by_provider,
        }


# ---------------------------------------------------------------------------
# The enrichment step
# ---------------------------------------------------------------------------


def _candidate_urls(paper) -> list[tuple[str, str | None]]:
    providers: list[tuple[str, str | None]] = []
    if paper.openaccess_pdf_url:
        providers.append(("semantic_scholar_oa", paper.openaccess_pdf_url))
    if paper.arxiv_id:
        providers.append(("arxiv", f"https://arxiv.org/pdf/{paper.arxiv_id}"))
    if paper.acl_id:
        providers.append(
            ("acl_anthology", f"https://aclanthology.org/{paper.acl_id}.pdf")
        )
    providers.append(("unpaywall", None))
    return providers


def _same_location(a: str, b: str) -> bool:
    def key(url: str) -> str:
        parts = urlsplit(url)
        host = (parts.hostname or "").removeprefix("www.").replace("export.", "")
        return f"{host}{parts.path.removesuffix('.pdf')}"

    return key(a) == key(b)


def acquire_missing_full_text(
    session: Session,
    log: AttemptLog,
    *,
    pdf_dir: str,
    unpaywall_email: str | None,
    download_arxiv: Callable[[str, str], DownloadResult],
    download_other: Callable[[str, str], DownloadResult],
    budget_seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Try every provider, in order, for every paper still without full text.

    Corpus-wide, as Unpaywall enrichment was: a paper an earlier run left
    metadata-only gets another chance. Papers the time budget does not reach
    are recorded ``not_attempted``. Returns the step's summary; the per-paper
    detail is in `fulltext_attempts`.
    """
    deadline = clock() + budget_seconds
    papers = list(
        session.scalars(
            select(corpus.Paper)
            .where(corpus.Paper.has_full_text.is_(False))
            .order_by(corpus.Paper.id)
        )
    )
    stored: list[int] = []
    budget_exhausted = False
    for paper in papers:
        if clock() >= deadline:
            budget_exhausted = True
            log.record(
                paper.id,
                "all",
                "enrichment",
                "not_attempted",
                retryable=True,
                detail="time_budget",
            )
            continue
        tried: list[str] = []
        dest = f"{pdf_dir}/{paper.id}.pdf"
        for provider, url in _candidate_urls(paper):
            location_found = None
            if provider == "unpaywall":
                if not paper.doi:
                    log.record(
                        paper.id, provider, "enrichment", "no_doi", retryable=False
                    )
                    continue
                if not unpaywall_email:
                    log.record(
                        paper.id,
                        provider,
                        "enrichment",
                        "not_configured",
                        retryable=False,
                    )
                    continue
                lookup = unpaywall.lookup_oa_pdf(paper.doi, unpaywall_email)
                if lookup.url is None:
                    outcome = _LOOKUP_OUTCOME.get(
                        lookup.status, "lookup_invalid_response"
                    )
                    log.record(
                        paper.id,
                        provider,
                        "enrichment",
                        outcome,
                        retryable=outcome in _RETRYABLE_LOOKUPS,
                        detail=f"http_{lookup.http_status}"
                        if lookup.http_status
                        else None,
                    )
                    continue
                url, location_found = lookup.url, True
            if any(_same_location(url, earlier) for earlier in tried):
                continue
            tried.append(url)
            fetch = download_arxiv if arxiv.is_arxiv_url(url) else download_other
            result = fetch(url, dest)
            log.record(
                paper.id,
                provider,
                "enrichment",
                result.status,
                result=result,
                url=url,
                location_found=location_found,
                pdf_path=dest if result.ok else None,
            )
            if result.ok:
                corpus.enrich_paper(
                    session, paper.id, {"has_full_text": True, "pdf_path": dest}
                )
                stored.append(paper.id)
                break
    return {
        "papers_considered": len(papers),
        "pdfs_stored": len(stored),
        "stored_paper_ids": stored,
        "budget_exhausted": budget_exhausted,
        **log.summary(),
    }
