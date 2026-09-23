"""POST /collect and POST /upload — Phase 1's public ingestion surface (D-20, D-21, D-22).

/collect orchestrates arXiv + Semantic Scholar + ACL Anthology discovery
through dedup.py, then Unpaywall enrichment, returning the count/ids of
papers touched (new inserts + enriched rows) for the run. Each discovery source
contributes at most its configured cap of distinct papers (10 by default and at
most); manual uploads are uncapped. The caps are per source and independent —
three sources answering in full store 30 papers; there is no run-wide cap.

The response also reports what the run actually did with what it found
(D-DUP-01): `added` rows created, a `duplicates` breakdown by category, and
`source_stats` per source (candidates returned, how many of them were processed
under that source's cap, rows created, duplicate candidates). So "30 candidates
found, 22 papers added" reads as eight recognised duplicates rather than eight
papers silently lost, and a source that stored nothing says whether it returned
nothing or returned only papers the corpus already had. `RunLedger` documents how the
categories are counted; the identity rules themselves are dedup.py's and are
unchanged.

Each source runs in isolation: a third-party outage degrades the run to the
sources that answered instead of aborting it. Semantic Scholar answers
unauthenticated callers with 429/500 routinely, and an unguarded exception
there used to roll back every arXiv row already written while the PDFs those
rows pointed at stayed on disk.

/upload accepts one or more PDFs via manual_upload.save_upload.
"""

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.security import require_api_key
from app.api.write_guard import exclusive_write
from app.core.config import get_settings
from app.ingestion import corpus, dedup, manual_upload, unpaywall
from app.ingestion.sources import acl_anthology, arxiv, semantic_scholar

logger = logging.getLogger(__name__)

# Both endpoints write to the corpus and reach out to external services, so the
# whole router sits behind the Phase 11 API key (README §12 shows /collect
# being called with X-API-Key). /health stays open — see app.api.security.
router = APIRouter(dependencies=[Depends(require_api_key), Depends(exclusive_write)])


class CollectRequest(BaseModel):
    topic: str


def _to_paper_dict(candidate: dict, source: str, pdf_path: str | None) -> dict:
    """Map a source's raw candidate dict onto exactly the Paper column set.

    Source modules return extra keys (`pdf_url`/`openaccess_pdf_url`) that
    are not Paper columns — this is the single place those get dropped.
    """
    return {
        "title": candidate.get("title"),
        "authors": candidate.get("authors"),
        "year": candidate.get("year"),
        "venue": candidate.get("venue"),
        "doi": candidate.get("doi"),
        "abstract": candidate.get("abstract"),
        "citation_count": candidate.get("citation_count"),
        "source": source,
        "pdf_path": pdf_path,
        "has_full_text": pdf_path is not None,
        "arxiv_id": candidate.get("arxiv_id"),
        "acl_id": candidate.get("acl_id"),
    }


_UPLOAD_CHUNK_SIZE = 1024 * 1024  # 1MB


def _read_bounded(file: UploadFile, max_bytes: int) -> bytes:
    """Read `file` in chunks, raising before buffering past `max_bytes`.

    A plain `file.file.read()` would buffer the entire upload into memory
    before manual_upload.save_upload's own size check ever runs, so an
    oversized upload could exhaust worker memory before being rejected.
    """
    chunks = []
    total = 0
    while True:
        chunk = file.file.read(_UPLOAD_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise manual_upload.UploadRejected(
                413, "Uploaded file exceeds the maximum allowed size"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _process_one(session: Session, paper_dict: dict) -> tuple[int, bool]:
    """Store one candidate, returning its corpus id and whether it was new.

    The identity rules are `dedup.find_existing`'s and are not re-stated here;
    this only reports which of the two branches ran, so the run can tell a
    newly inserted paper from a duplicate that merged onto an existing row.
    """
    existing = dedup.find_existing(session, paper_dict)
    if existing:
        updates = dedup.merge(existing, paper_dict)
        corpus.enrich_paper(session, existing.id, updates)
        return existing.id, False
    paper = corpus.insert_paper(session, paper_dict)
    return paper.id, True


@dataclass
class SourceOutcome:
    """What one discovery source produced, before the run merges it in.

    Held separately per source rather than written straight into the run's
    totals because a source that raises has its rows rolled back to its own
    savepoint — its counts must be discarded with them (see `_run`).
    """

    # Everything the source returned, before the cap and before dedup.
    candidates: int = 0
    # How many of those were actually written — fewer when the cap stopped the
    # run early. The two apart are what tell "returned nothing" from "returned
    # plenty, kept ten".
    processed: int = 0
    kept: set[int] = field(default_factory=set)
    inserted: set[int] = field(default_factory=set)
    # One entry per duplicate *encounter*: a candidate that merged onto a row
    # that already existed, whether from this run or an earlier one.
    duplicates: list[int] = field(default_factory=list)


def _process_capped(
    session: Session,
    candidates: Sequence[dict],
    source: str,
    limit: int,
    fetch_pdf: Callable[[dict], str | None] | None = None,
) -> SourceOutcome:
    """Write `source`'s candidates until `limit` distinct corpus rows are touched.

    The cap counts papers, not candidates: a candidate that dedups onto a row
    this source already touched is merged as usual but does not use up a slot.
    A source with fewer matches simply returns fewer. The check runs before a
    candidate's PDF is fetched, so nothing past the cap is downloaded.

    The cap is this source's alone — each source gets its own `SourceOutcome`
    and its own limit, so three sources answering in full store three sources'
    worth of papers. There is no run-wide cap anywhere.
    """
    outcome = SourceOutcome(candidates=len(candidates))
    for candidate in candidates:
        if len(outcome.kept) >= limit:
            break
        outcome.processed += 1
        pdf_path = fetch_pdf(candidate) if fetch_pdf is not None else None
        paper_id, inserted = _process_one(
            session, _to_paper_dict(candidate, source, pdf_path)
        )
        outcome.kept.add(paper_id)
        if inserted:
            outcome.inserted.add(paper_id)
        else:
            outcome.duplicates.append(paper_id)
    return outcome


class RunLedger:
    """Per-run bookkeeping behind /collect's `added`/`duplicates` reporting (D-DUP-01).

    Duplicate *categories* are counted per distinct paper, never per encounter:
    one paper that arXiv, Semantic Scholar and ACL all returned is a single
    cross-source duplicate, not three. Each duplicated paper falls in exactly
    one category, in this priority:

    1. `already_in_corpus` — an earlier run had already stored it. This wins
       over the others because it is the fact the user is actually asking about
       when a search "finds" papers that do not appear as new.
    2. `cross_source` — this run inserted it, and more than one source
       returned it.
    3. `within_source` — a single source returned the same paper twice.

    A paper can be both newly inserted and duplicated (source A inserts it,
    source B re-finds it): `added` counts rows created, `duplicates` counts
    papers that were recognised rather than re-created.
    """

    def __init__(self) -> None:
        self.touched: set[int] = set()
        self.inserted: set[int] = set()
        self.duplicated: set[int] = set()
        self.sources_by_paper: dict[int, set[str]] = {}
        self.stats: dict[str, dict[str, int]] = {}

    def record(self, source: str, outcome: SourceOutcome) -> None:
        self.touched |= outcome.kept
        self.inserted |= outcome.inserted
        self.duplicated.update(outcome.duplicates)
        for paper_id in outcome.kept:
            self.sources_by_paper.setdefault(paper_id, set()).add(source)
        self.stats[source] = {
            "candidates": outcome.candidates,
            "processed": outcome.processed,
            "added": len(outcome.inserted),
            "duplicates": len(outcome.duplicates),
        }
        # One line per source per run, so a run that stored nothing from a
        # source says whether the source returned nothing or returned only
        # papers the corpus already had — the two look identical in a bare
        # `"status": "ok"`.
        logger.info(
            "Collection source=%s candidates=%d processed=%d added=%d "
            "duplicate_candidates=%d",
            source,
            outcome.candidates,
            outcome.processed,
            len(outcome.inserted),
            len(outcome.duplicates),
        )

    def enriched(self, paper_ids: Iterable[int]) -> None:
        """Unpaywall touches rows discovery already wrote; it creates none."""
        self.touched |= set(paper_ids)

    def duplicate_summary(self) -> dict[str, int]:
        cross_source = within_source = already_in_corpus = 0
        for paper_id in self.duplicated:
            if paper_id not in self.inserted:
                already_in_corpus += 1
            elif len(self.sources_by_paper.get(paper_id, ())) > 1:
                cross_source += 1
            else:
                within_source += 1
        return {
            "total": len(self.duplicated),
            "cross_source": cross_source,
            "within_source": within_source,
            "already_in_corpus": already_in_corpus,
        }


DISCOVERY_SOURCES = ("arxiv", "semantic_scholar", "acl_anthology")


def _failure_status(exc: Exception) -> str:
    """A short, non-leaking description of why a source did not answer."""
    detail = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"failed: {type(exc).__name__}{f': {detail[:200]}' if detail else ''}"


@router.post("/collect")
def collect(
    body: CollectRequest,
    session: Session = Depends(corpus.get_session),  # noqa: B008 — required FastAPI DI pattern
):
    settings = get_settings()
    ledger = RunLedger()
    statuses: dict[str, str] = {}

    def _download_arxiv_pdf(url: str, dest_path: str) -> bool:
        """Every arXiv-hosted PDF this run fetches, from discovery or Unpaywall.

        One downloader for both so the run's configured contact, spacing and
        retry budget reach the full-text path as well as the API path, and so
        all of it queues on arXiv's single process-wide limiter (D-PDF-01).
        """
        return arxiv.download_pdf(
            url,
            dest_path,
            contact=settings.arxiv_contact,
            num_retries=settings.arxiv_num_retries,
            min_interval_seconds=settings.arxiv_min_interval_seconds,
            backoff_base_seconds=settings.arxiv_backoff_base_seconds,
            backoff_max_seconds=settings.arxiv_backoff_max_seconds,
        )

    def _run(name: str, step) -> None:
        """Run one ingestion step, recording rather than propagating its failure.

        Each step is independent, so one source's outage must not discard what
        the others already wrote. Anything the step did before raising is rolled
        back to its own savepoint; earlier sources keep their rows.
        """
        savepoint = session.begin_nested()
        try:
            step()
        except Exception as exc:
            savepoint.rollback()
            statuses[name] = _failure_status(exc)
            logger.warning("Ingestion source %r failed: %s", name, exc, exc_info=True)
        else:
            savepoint.commit()
            statuses[name] = "ok"

    def _arxiv() -> None:
        candidates = arxiv.search_topic(
            body.topic,
            max_results=settings.arxiv_max_results,
            num_retries=settings.arxiv_num_retries,
            min_interval_seconds=settings.arxiv_min_interval_seconds,
            backoff_base_seconds=settings.arxiv_backoff_base_seconds,
            backoff_max_seconds=settings.arxiv_backoff_max_seconds,
            contact=settings.arxiv_contact,
        )
        ledger.record(
            "arxiv",
            _process_capped(
                session,
                candidates,
                "arxiv",
                settings.arxiv_max_results,
                lambda c: arxiv.fetch_pdf(
                    c, settings.pdf_storage_path, download=_download_arxiv_pdf
                ),
            ),
        )

    def _semantic_scholar() -> None:
        candidates = semantic_scholar.search_topic(
            body.topic,
            settings.semantic_scholar_api_key,
            limit=settings.semantic_scholar_max_results,
            request_limit=settings.semantic_scholar_request_limit,
            max_retries=settings.semantic_scholar_max_retries,
            min_interval_seconds=settings.semantic_scholar_min_interval_seconds,
            backoff_base_seconds=settings.semantic_scholar_backoff_base_seconds,
            backoff_max_seconds=settings.semantic_scholar_backoff_max_seconds,
        )
        ledger.record(
            "semantic_scholar",
            _process_capped(
                session,
                candidates,
                "semantic_scholar",
                settings.semantic_scholar_max_results,
            ),
        )

    def _acl_anthology() -> None:
        acl_anthology.download_anthology_export(settings.acl_anthology_bib_path)
        candidates = acl_anthology.search_topic(
            settings.acl_anthology_bib_path, body.topic
        )
        ledger.record(
            "acl_anthology",
            _process_capped(
                session,
                candidates,
                "acl_anthology",
                settings.acl_anthology_max_results,
                lambda c: acl_anthology.fetch_pdf(c, settings.pdf_storage_path),
            ),
        )

    def _unpaywall() -> None:
        enriched = unpaywall.enrich(
            session,
            settings.unpaywall_email,
            settings.pdf_storage_path,
            download_arxiv_pdf=_download_arxiv_pdf,
        )
        ledger.enriched(enriched)
        logger.info("Collection source=unpaywall enriched=%d", len(enriched))

    _run("arxiv", _arxiv)
    _run("semantic_scholar", _semantic_scholar)
    _run("acl_anthology", _acl_anthology)
    # Enrichment runs last and only over rows discovery already wrote.
    _run("unpaywall", _unpaywall)

    if all(statuses[name] != "ok" for name in DISCOVERY_SOURCES):
        session.rollback()
        raise HTTPException(
            status_code=502,
            detail="No discovery source could be reached. Check network access.",
        )

    session.commit()

    duplicates = ledger.duplicate_summary()
    logger.info(
        "Collection complete topic=%r papers=%d added=%d duplicates=%s",
        body.topic,
        len(ledger.touched),
        len(ledger.inserted),
        duplicates,
    )
    return {
        "count": len(ledger.touched),
        "ids": list(ledger.touched),
        "added": len(ledger.inserted),
        "duplicates": duplicates,
        "sources": statuses,
        "source_stats": ledger.stats,
    }


@router.post("/upload")
def upload(
    files: list[UploadFile],
    session: Session = Depends(corpus.get_session),  # noqa: B008 — required FastAPI DI pattern
):
    settings = get_settings()
    max_bytes = settings.max_upload_size_mb * 1024 * 1024

    created = []
    for file in files:
        try:
            file_bytes = _read_bounded(file, max_bytes)
            result = manual_upload.save_upload(
                file_bytes=file_bytes,
                filename=file.filename,
                content_type=file.content_type,
                dest_dir=settings.pdf_storage_path,
                max_bytes=max_bytes,
                session=session,
            )
        except manual_upload.UploadRejected as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        created.append(result["id"])

    session.commit()

    return {"created": created}
