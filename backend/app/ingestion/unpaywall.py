"""Unpaywall enrichment (D-17, INGEST-06) — resolves open-access full text for
corpus rows that have a DOI but no full-text PDF, and UPDATEs the existing row
(never inserts a new one — Pattern 3 in 01-RESEARCH.md).

Distinguishes "no OA copy" (200, best_oa_location=null) from "DOI not found"
(non-200, often a plain-HTML body) per D-17 and 01-RESEARCH.md's Pitfall 2 —
the status_code guard runs before any .json() call.
"""

from collections.abc import Callable

import httpx
from sqlalchemy.orm import Session

from app.ingestion import corpus
from app.ingestion.sources import arxiv


def resolve_oa_pdf(doi: str, email: str) -> str | None:
    try:
        with httpx.Client(transport=httpx.HTTPTransport(retries=2)) as client:
            resp = client.get(
                f"https://api.unpaywall.org/v2/{doi}",
                params={"email": email},
                timeout=10.0,
            )
    except httpx.HTTPError:
        # Unreachable this run means no copy was found, not a failure. `enrich`
        # also runs inside Phase 2's manual-upload backfill, where an escaping
        # error aborts the whole `/pipeline/run`; in `/collect` it would roll
        # back every enrichment already made in the same step.
        return None
    if resp.status_code != 200:
        return None  # 404 = DOI not found in Unpaywall's index; not "no OA"
    try:
        body = resp.json()
    except ValueError:
        return None  # a 200 that is not JSON carries no location to act on
    best = body.get("best_oa_location")
    if not best:
        return None  # valid DOI, genuinely no OA copy
    return best.get("url_for_pdf") or best.get("url")


def enrich(
    session: Session,
    email: str,
    dest_dir: str,
    download_arxiv_pdf: Callable[[str, str], bool] | None = None,
    *,
    paper_id: int | None = None,
) -> list[int]:
    """Resolve and store an OA full text for every DOI-bearing row that lacks one.

    An OA copy Unpaywall resolves to arXiv is an arXiv request like any other
    (D-PDF-01), so it goes through `arxiv.download_pdf` — paced by arXiv's
    process-wide limiter and carrying the ScholarLens User-Agent — while every
    other OA host keeps the shared downloader. `download_arxiv_pdf` lets
    `/collect` inject a downloader carrying that run's Settings; the default
    stays polite so no call site can silently fall back to an unpaced fetch.
    """
    download_arxiv = (
        arxiv.download_pdf if download_arxiv_pdf is None else download_arxiv_pdf
    )
    enriched_ids: list[int] = []
    for row in corpus.rows_missing_fulltext(session):
        if paper_id is not None and row.id != paper_id:
            continue
        url = resolve_oa_pdf(row.doi, email)
        if url is None:
            continue
        dest_path = f"{dest_dir}/{row.id}.pdf"
        fetch = download_arxiv if arxiv.is_arxiv_url(url) else corpus.download_pdf
        if not fetch(url, dest_path):
            continue
        corpus.enrich_paper(
            session, row.id, {"has_full_text": True, "pdf_path": dest_path}
        )
        enriched_ids.append(row.id)
    return enriched_ids
