"""POST /collect and POST /upload — Phase 1's public ingestion surface (D-20, D-21, D-22).

/collect orchestrates arXiv + Semantic Scholar + ACL Anthology discovery
through dedup.py, then Unpaywall enrichment, returning the count/ids of
papers touched (new inserts + enriched rows) for the run.

/upload accepts one or more PDFs via manual_upload.save_upload.
"""

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.security import require_api_key
from app.core.config import get_settings
from app.ingestion import corpus, dedup, manual_upload, unpaywall
from app.ingestion.sources import acl_anthology, arxiv, semantic_scholar

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


@router.post("/collect")
def collect(
    body: CollectRequest,
    session: Session = Depends(corpus.get_session),  # noqa: B008 — required FastAPI DI pattern
):
    settings = get_settings()
    touched: set[int] = set()

    for candidate in arxiv.search_topic(body.topic):
        pdf_path = arxiv.fetch_pdf(candidate, settings.pdf_storage_path)
        touched.add(_process_one(session, _to_paper_dict(candidate, "arxiv", pdf_path)))

    for candidate in semantic_scholar.search_topic(
        body.topic, settings.semantic_scholar_api_key
    ):
        touched.add(
            _process_one(session, _to_paper_dict(candidate, "semantic_scholar", None))
        )

    acl_anthology.download_anthology_export(settings.acl_anthology_bib_path)
    for candidate in acl_anthology.search_topic(
        settings.acl_anthology_bib_path, body.topic
    ):
        pdf_path = acl_anthology.fetch_pdf(candidate, settings.pdf_storage_path)
        touched.add(
            _process_one(session, _to_paper_dict(candidate, "acl_anthology", pdf_path))
        )

    enriched_ids = unpaywall.enrich(
        session, settings.unpaywall_email, settings.pdf_storage_path
    )
    touched |= set(enriched_ids)

    session.commit()

    return {"count": len(touched), "ids": list(touched)}


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
