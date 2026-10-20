"""Semantic Scholar ingestion source (Plan 01-03, INGEST-04).

Queries the public Semantic Scholar Graph API (`/graph/v1/paper/search`) for
a topic string and maps the response onto the corpus-shaped dict contract.

Strictly metadata-only per README §8 and D-14: this module never downloads
a PDF. If `openAccessPdf.url` is present in the response it is stored as
`openaccess_pdf_url` for the Unpaywall enrichment step (Plan 01-05) — or any
other later full-text-recovery step — to act on.

This module does not read Settings itself; the caller (Plan 01-06's
`/collect` orchestration) passes `api_key` and the request limits explicitly
so this stays a pure/testable function per RESEARCH.md's "source module
returns plain dicts" pattern.

Every request, retries included, waits its turn on the process-wide `LIMITER`
(see `app.ingestion.rate_limit`).

D-S2-TYPES-01: records Semantic Scholar's structured `publicationTypes` field
marks as clearly non-research (an editorial, a news item, a letter/comment, a
case report) are dropped here, before they become candidates — the same
boundary ACL Anthology's tutorial/demo filter uses — so they can never occupy
a `semantic_scholar_max_results` slot. The decision reads that metadata field
only; titles, abstracts and PDFs are never inspected.
"""

import logging

import httpx

from app.ingestion.rate_limit import RateLimiter, send_with_retries
from app.ingestion.retrieval import Candidates, error_category
from app.ingestion.retrieval import report as retrieval_report

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
FIELDS = (
    "paperId,url,title,authors,year,venue,externalIds,abstract,openAccessPdf,"
    "citationCount,publicationTypes"
)

# The search endpoint's largest accepted `limit`.
MAX_PAGE_SIZE = 100
# Relevance search serves at most this many results (offset + limit).
MAX_RELEVANCE_RESULTS = 1000

# Publication types that identify a document with no original research.
NON_RESEARCH_TYPES = frozenset(
    {"Editorial", "News", "LettersAndComments", "CaseReport"}
)
# Types that say the record *is* research content. `JournalArticle` and
# `Conference` are deliberately absent: they name where a record appeared, not
# what it is, so an editorial printed in a journal is still an editorial.
RESEARCH_CONTENT_TYPES = frozenset({"Study", "Review"})

# One limiter for every Semantic Scholar request this process makes. Each call
# sets its interval from Settings; this default only applies until then.
LIMITER = RateLimiter(min_interval_seconds=1.5)


def is_non_research(publication_types: list[str] | None) -> bool:
    """Whether Semantic Scholar's own type tags mark a record as non-research.

    Missing, null or empty types are never rejected — absence of a tag is not
    evidence. A record carrying a non-research tag is kept when it is also
    tagged as a study or review, so only *clearly* non-research records go.
    """
    types = set(publication_types or ())
    return bool(types & NON_RESEARCH_TYPES) and not types & RESEARCH_CONTENT_TYPES


def _candidate(p: dict) -> dict:
    external_ids = p.get("externalIds") or {}
    open_access = p.get("openAccessPdf") or {}
    return {
        "title": p.get("title"),
        "authors": [a["name"] for a in p.get("authors") or [] if a.get("name")],
        "year": p.get("year"),
        "venue": p.get("venue") or None,
        "doi": external_ids.get("DOI"),
        # Kept for dedup (S1 audit, 2026-10-10): without them an S2
        # copy of an arXiv or Anthology paper is matched only by DOI
        # or by an exact title in the exact same year.
        "arxiv_id": external_ids.get("ArXiv"),
        "acl_id": external_ids.get("ACL"),
        "abstract": p.get("abstract"),
        # Phase 8's `citation_impact` signal. Left as None when the
        # response omits it — an unknown count is not a count of zero.
        "citation_count": p.get("citationCount"),
        # The API answers `{"url": "", "status": null}` when it knows no
        # open-access copy (verified 2026-10-12): an empty URL is no URL.
        "openaccess_pdf_url": open_access.get("url") or None,
        "openaccess_status": open_access.get("status"),
        "s2_paper_id": p.get("paperId"),
        "source_url": p.get("url"),
    }


def search_topic(
    topic: str,
    api_key: str | None = None,
    limit: int = 10,
    max_retries: int = 3,
    min_interval_seconds: float = 1.5,
    backoff_base_seconds: float = 2.0,
    backoff_max_seconds: float = 30.0,
    limiter: RateLimiter | None = None,
    request_limit: int = 20,
    page_size: int | None = None,
    max_pages: int | None = None,
) -> Candidates:
    """Search Semantic Scholar for `topic` and return corpus-shaped dicts.

    Each dict has keys: title, authors (list[str] of names), year, venue,
    doi, arxiv_id, acl_id, abstract, citation_count, openaccess_pdf_url (URL
    only — never downloaded here), openaccess_status, s2_paper_id and
    source_url (the paper's Semantic Scholar page).

    **Paging (D-S1-RETRIEVAL-01).** Pages of `page_size` records (default
    `max(limit, request_limit)`, at most the API's 100) are requested in
    Semantic Scholar's relevance order, following the response's `next`
    offset, until `limit` research records are kept, the result set is
    exhausted, `max_pages` pages were spent (default: the pages the pool
    needs plus one for backfill) or the API's 1,000-result ceiling for
    relevance search is reached. Non-research records (D-S2-TYPES-01),
    records with no title, and repeats of a paper already read are dropped and
    backfilled from later pages. `Candidates.retrieval` reports the source's
    `total`, the pages requested, the records fetched and dropped, and why
    reading stopped; a failed later page keeps what was read and is reported
    incomplete. A failure on the first page still raises.

    A 429 or 5xx answer is retried through the shared limiter (see
    `send_with_retries`); the last answer's error is raised once retrying
    stops, and `CooldownActive` while an earlier refusal's cooldown runs.
    `limiter` exists for tests; callers use the process-wide `LIMITER`.
    """
    limiter = LIMITER if limiter is None else limiter
    limiter.set_min_interval(min_interval_seconds)
    headers = {"x-api-key": api_key} if api_key else {}
    size = min(page_size or max(limit, request_limit), MAX_PAGE_SIZE)
    if max_pages is None:
        max_pages = -(-limit // size) + 1

    results: list[dict] = []
    seen: set[str] = set()
    dropped = {"non_research": 0, "duplicate_across_pages": 0, "no_title": 0}
    total: int | None = None
    offset = pages_requested = pages_succeeded = fetched = 0
    stop_reason = "exhausted"
    error: str | None = None

    # The transport's own `retries` covers only failed connections, never an
    # HTTP status — rate limiting is handled by send_with_retries.
    with httpx.Client(transport=httpx.HTTPTransport(retries=2)) as client:
        while True:
            if pages_requested >= max_pages:
                stop_reason = "page_budget"
                break
            if offset >= MAX_RELEVANCE_RESULTS:
                stop_reason = "ceiling"
                break
            params = {
                "query": topic,
                "fields": FIELDS,
                "limit": min(size, MAX_RELEVANCE_RESULTS - offset),
                "offset": offset,
            }
            pages_requested += 1
            try:
                resp = send_with_retries(
                    lambda params=params: client.get(
                        SEARCH_URL, params=params, headers=headers, timeout=10.0
                    ),
                    limiter=limiter,
                    source="Semantic Scholar",
                    max_retries=max_retries,
                    backoff_base_seconds=backoff_base_seconds,
                    backoff_max_seconds=backoff_max_seconds,
                )
                resp.raise_for_status()
                body = resp.json()
                if not isinstance(body, dict):
                    raise TypeError("Semantic Scholar answered a non-object body")
            except Exception as exc:
                if not pages_succeeded:
                    raise
                stop_reason, error = "page_error", error_category(exc)
                logger.warning(
                    "Semantic Scholar page at offset %d failed (%s); keeping %d",
                    offset,
                    error,
                    len(results),
                )
                break
            pages_succeeded += 1
            if total is None and isinstance(body.get("total"), int):
                total = body["total"]
            data = body.get("data") or []
            fetched += len(data)
            for p in data:
                if len(results) >= limit:
                    break
                if not p.get("title"):
                    dropped["no_title"] += 1
                    continue
                key = p.get("paperId") or " ".join(p["title"].lower().split())
                if key in seen:
                    dropped["duplicate_across_pages"] += 1
                    continue
                seen.add(key)
                if is_non_research(p.get("publicationTypes")):
                    dropped["non_research"] += 1
                    continue
                results.append(_candidate(p))
            nxt = body.get("next")
            more = bool(data) and isinstance(nxt, int) and nxt > offset
            if more and total is not None and nxt >= total:
                more = False
            if len(results) >= limit:
                stop_reason = "ceiling" if more else "exhausted"
                break
            if not more:
                break
            offset = nxt
    if dropped["non_research"]:
        logger.info(
            "Semantic Scholar: skipped %d non-research record(s) by publicationTypes",
            dropped["non_research"],
        )
    return Candidates(
        results,
        retrieval_report(
            query=topic,
            params={"pool": limit, "page_size": size, "max_pages": max_pages},
            total_reported=total,
            pages_requested=pages_requested,
            pages_succeeded=pages_succeeded,
            records_fetched=fetched,
            records_dropped=dropped,
            candidates=len(results),
            stop_reason=stop_reason,
            error=error,
        ),
    )
