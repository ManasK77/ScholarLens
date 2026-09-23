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

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
FIELDS = (
    "title,authors,year,venue,externalIds,abstract,openAccessPdf,citationCount,"
    "publicationTypes"
)

# The search endpoint's largest accepted `limit`.
MAX_PAGE_SIZE = 100

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
) -> list[dict]:
    """Search Semantic Scholar for `topic` and return corpus-shaped dicts.

    Each dict has keys: title, authors (list[str] of names), year, venue,
    doi, abstract, citation_count, openaccess_pdf_url (URL only — never
    downloaded here).

    One page of `request_limit` results (never fewer than `limit`, never more
    than the API allows) is requested, non-research records are dropped, and
    the first `limit` that remain are returned in Semantic Scholar's relevance
    order — so a dropped record does not cost a slot, and filtering never adds
    a request.

    A 429 or 5xx answer is retried through the shared limiter (see
    `send_with_retries`); the last answer's error is raised once retrying
    stops, and `CooldownActive` while an earlier refusal's cooldown runs.
    `limiter` exists for tests; callers use the process-wide `LIMITER`.
    """
    limiter = LIMITER if limiter is None else limiter
    limiter.set_min_interval(min_interval_seconds)
    headers = {"x-api-key": api_key} if api_key else {}
    page_size = min(max(limit, request_limit), MAX_PAGE_SIZE)
    params = {"query": topic, "fields": FIELDS, "limit": page_size}

    # The transport's own `retries` covers only failed connections, never an
    # HTTP status — rate limiting is handled by send_with_retries.
    with httpx.Client(transport=httpx.HTTPTransport(retries=2)) as client:
        resp = send_with_retries(
            lambda: client.get(
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

    results = []
    rejected = 0
    for p in body.get("data", []):
        if len(results) >= limit:
            break
        if is_non_research(p.get("publicationTypes")):
            rejected += 1
            continue
        results.append(
            {
                "title": p.get("title"),
                "authors": [a["name"] for a in p.get("authors", [])],
                "year": p.get("year"),
                "venue": p.get("venue") or None,
                "doi": (p.get("externalIds") or {}).get("DOI"),
                "abstract": p.get("abstract"),
                # Phase 8's `citation_impact` signal. Left as None when the
                # response omits it — an unknown count is not a count of zero.
                "citation_count": p.get("citationCount"),
                "openaccess_pdf_url": (p.get("openAccessPdf") or {}).get("url"),
            }
        )
    if rejected:
        logger.info(
            "Semantic Scholar: skipped %d non-research record(s) by publicationTypes",
            rejected,
        )
    return results
