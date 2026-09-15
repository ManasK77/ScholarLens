"""POST /collect and POST /upload — Phase 1's public ingestion surface (D-20, D-21, D-22).

/collect orchestrates arXiv + Semantic Scholar + ACL Anthology discovery
through dedup.py, then Unpaywall enrichment, returning the count/ids of
papers touched (new inserts + enriched rows) for the run. Each discovery source
contributes at most its configured cap of distinct papers (10 by default and at
most); manual uploads are uncapped.

Each source runs in isolation: a third-party outage degrades the run to the
sources that answered instead of aborting it. Semantic Scholar answers
unauthenticated callers with 429/500 routinely, and an unguarded exception
there used to roll back every arXiv row already written while the PDFs those
rows pointed at stayed on disk.

/upload accepts one or more PDFs via manual_upload.save_upload.
"""

import logging
from collections.abc import Callable, Iterable

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.security import require_api_key
from app.core.config import get_settings
from app.ingestion import corpus, dedup, manual_upload, unpaywall
from app.ingestion.sources import acl_anthology, arxiv, semantic_scholar

logger = logging.getLogger(__name__)

# Both endpoints write to the corpus and reach out to external services, so the
# whole router sits behind the Phase 11 API key (README §12 shows /collect
# being called with X-API-Key). /health stays open — see app.api.security.
router = APIRouter(dependencies=[Depends(require_api_key)])


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


def _process_one(session: Session, paper_dict: dict) -> int:
    existing = dedup.find_existing(session, paper_dict)
    if existing:
        updates = dedup.merge(existing, paper_dict)
        corpus.enrich_paper(session, existing.id, updates)
        return existing.id
    paper = corpus.insert_paper(session, paper_dict)
    return paper.id


def _process_capped(
    session: Session,
    candidates: Iterable[dict],
    source: str,
    limit: int,
    fetch_pdf: Callable[[dict], str | None] | None = None,
) -> set[int]:
    """Write `source`'s candidates until `limit` distinct corpus rows are touched.

    The cap counts papers, not candidates: a candidate that dedups onto a row
    this source already touched is merged as usual but does not use up a slot.
    A source with fewer matches simply returns fewer. The check runs before a
    candidate's PDF is fetched, so nothing past the cap is downloaded.
    """
    kept: set[int] = set()
    for candidate in candidates:
        if len(kept) >= limit:
            break
        pdf_path = fetch_pdf(candidate) if fetch_pdf is not None else None
        kept.add(_process_one(session, _to_paper_dict(candidate, source, pdf_path)))
    return kept


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
    touched: set[int] = set()
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
        touched.update(
            _process_capped(
                session,
                candidates,
                "arxiv",
                settings.arxiv_max_results,
                lambda c: arxiv.fetch_pdf(
                    c, settings.pdf_storage_path, download=_download_arxiv_pdf
                ),
            )
        )

    def _semantic_scholar() -> None:
        candidates = semantic_scholar.search_topic(
            body.topic,
            settings.semantic_scholar_api_key,
            limit=settings.semantic_scholar_max_results,
            max_retries=settings.semantic_scholar_max_retries,
            min_interval_seconds=settings.semantic_scholar_min_interval_seconds,
            backoff_base_seconds=settings.semantic_scholar_backoff_base_seconds,
            backoff_max_seconds=settings.semantic_scholar_backoff_max_seconds,
        )
        touched.update(
            _process_capped(
                session,
                candidates,
                "semantic_scholar",
                settings.semantic_scholar_max_results,
            )
        )

    def _acl_anthology() -> None:
        acl_anthology.download_anthology_export(settings.acl_anthology_bib_path)
        candidates = acl_anthology.search_topic(
            settings.acl_anthology_bib_path, body.topic
        )
        touched.update(
            _process_capped(
                session,
                candidates,
                "acl_anthology",
                settings.acl_anthology_max_results,
                lambda c: acl_anthology.fetch_pdf(c, settings.pdf_storage_path),
            )
        )

    def _unpaywall() -> None:
        touched.update(
            unpaywall.enrich(
                session,
                settings.unpaywall_email,
                settings.pdf_storage_path,
                download_arxiv_pdf=_download_arxiv_pdf,
            )
        )

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

    return {"count": len(touched), "ids": list(touched), "sources": statuses}


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
