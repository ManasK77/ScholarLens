"""Unpaywall enrichment (D-17, INGEST-06) — resolves open-access full text for
corpus rows that have a DOI but no full-text PDF, and UPDATEs the existing row
(never inserts a new one — Pattern 3 in 01-RESEARCH.md).

Distinguishes "no OA copy" (200, best_oa_location=null) from "DOI not found"
(non-200, often a plain-HTML body) per D-17 and 01-RESEARCH.md's Pitfall 2 —
the status_code guard runs before any .json() call.

**Outcomes (Phase 1 closeout, 2026-10-12).** `resolve_oa_pdf` answers None for
several different situations; `lookup_oa_pdf` names the one that occurred and
`enrich` counts them per row it looks up, so a run reports *why* rows stayed
metadata-only. Lookup outcomes: ``oa_location_found``, ``no_oa_copy`` (200,
no best location), ``doi_not_found`` (404), ``http_error`` (any other
non-200), ``invalid_response`` (a 200 whose body is not a JSON object, or
whose best location has no URL), ``timeout``, ``network_error`` (any other
transport failure). A found location then ends as ``oa_pdf_stored`` or
``download_failed`` (the download failed or the body was not a PDF; the
downloaders do not say which). Nothing logged carries the contact email: a
transport error is logged by class name only, since its message can include
the request URL and its ``email`` parameter.
"""

import logging
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from app.ingestion import corpus
from app.ingestion.sources import arxiv

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OaLookup:
    """What one Unpaywall lookup found; `url` is set only when one was found."""

    status: str
    url: str | None = None
    http_status: int | None = None


def lookup_oa_pdf(doi: str, email: str) -> OaLookup:
    try:
        with httpx.Client(transport=httpx.HTTPTransport(retries=2)) as client:
            resp = client.get(
                f"https://api.unpaywall.org/v2/{doi}",
                params={"email": email},
                timeout=10.0,
            )
    except httpx.TimeoutException:
        return OaLookup("timeout")
    except httpx.HTTPError:
        # Unreachable this run means no copy was found, not a failure. `enrich`
        # also runs inside Phase 2's manual-upload backfill, where an escaping
        # error aborts the whole `/pipeline/run`; in `/collect` it would roll
        # back every enrichment already made in the same step.
        return OaLookup("network_error")
    if resp.status_code == 404:
        # DOI not found in Unpaywall's index; not "no OA".
        return OaLookup("doi_not_found", http_status=404)
    if resp.status_code != 200:
        return OaLookup("http_error", http_status=resp.status_code)
    try:
        body = resp.json()
    except ValueError:
        return OaLookup("invalid_response", http_status=200)  # a 200 that is not JSON
    if not isinstance(body, dict):
        return OaLookup("invalid_response", http_status=200)
    best = body.get("best_oa_location")
    if not best:
        return OaLookup("no_oa_copy", http_status=200)  # valid DOI, genuinely no OA
    url = (
        (best.get("url_for_pdf") or best.get("url")) if isinstance(best, dict) else None
    )
    if not url:
        return OaLookup("invalid_response", http_status=200)
    return OaLookup("oa_location_found", url, http_status=200)


def resolve_oa_pdf(doi: str, email: str) -> str | None:
    """The OA copy's URL, or None whatever the reason (see `lookup_oa_pdf`)."""
    return lookup_oa_pdf(doi, email).url


def enrich(
    session: Session,
    email: str,
    dest_dir: str,
    download_arxiv_pdf: Callable[[str, str], bool] | None = None,
    *,
    paper_id: int | None = None,
    outcomes: MutableMapping[str, int] | None = None,
) -> list[int]:
    """Resolve and store an OA full text for every DOI-bearing row that lacks one.

    An OA copy Unpaywall resolves to arXiv is an arXiv request like any other
    (D-PDF-01), so it goes through `arxiv.download_pdf` — paced by arXiv's
    process-wide limiter and carrying the ScholarLens User-Agent — while every
    other OA host keeps the shared downloader. `download_arxiv_pdf` lets
    `/collect` inject a downloader carrying that run's Settings; the default
    stays polite so no call site can silently fall back to an unpaced fetch.

    `outcomes`, when given, counts one outcome per row looked up (see the
    module docstring); rows not looked up are not counted.
    """
    counts: dict[str, int] = {}

    def count(paper: int, outcome: str) -> None:
        counts[outcome] = counts.get(outcome, 0) + 1
        logger.debug("Unpaywall paper=%d outcome=%s", paper, outcome)

    download_arxiv = (
        arxiv.download_pdf if download_arxiv_pdf is None else download_arxiv_pdf
    )
    enriched_ids: list[int] = []
    for row in corpus.rows_missing_fulltext(session):
        if paper_id is not None and row.id != paper_id:
            continue
        found = lookup_oa_pdf(row.doi, email)
        if found.url is None:
            count(row.id, found.status)
            continue
        url = found.url
        dest_path = f"{dest_dir}/{row.id}.pdf"
        fetch = download_arxiv if arxiv.is_arxiv_url(url) else corpus.download_pdf
        if not fetch(url, dest_path):
            count(row.id, "download_failed")
            continue
        corpus.enrich_paper(
            session, row.id, {"has_full_text": True, "pdf_path": dest_path}
        )
        count(row.id, "oa_pdf_stored")
        enriched_ids.append(row.id)
    if outcomes is not None:
        for outcome, n in counts.items():
            outcomes[outcome] = outcomes.get(outcome, 0) + n
    if counts:
        logger.info(
            "Unpaywall looked up %d row(s): %s",
            sum(counts.values()),
            dict(sorted(counts.items())),
        )
    return enriched_ids
