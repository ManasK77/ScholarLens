"""Manual PDF upload ingestion (D-18, INGEST-07).

Never trusts a client-supplied filename for the on-disk path (path-traversal
mitigation) — writes are always to a server-generated `{paper_id}.pdf`.
Metadata fields are left `None`; GROBID backfills them in Phase 2.
"""

from pathlib import Path

from sqlalchemy.orm import Session

from app.ingestion import corpus


class UploadRejected(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def save_upload(
    file_bytes: bytes,
    filename: str,
    content_type: str,
    dest_dir: str,
    max_bytes: int,
    session: Session,
) -> dict:
    if content_type != "application/pdf":
        raise UploadRejected(415, f"Unsupported content type: {content_type}")

    if len(file_bytes) > max_bytes:
        raise UploadRejected(413, "Uploaded file exceeds the maximum allowed size")

    if not file_bytes.startswith(b"%PDF-"):
        raise UploadRejected(415, "Uploaded content is not a valid PDF file")

    paper = corpus.insert_paper(
        session,
        {
            "title": None,
            "authors": None,
            "year": None,
            "venue": None,
            "doi": None,
            "abstract": None,
            "source": "manual_upload",
            "pdf_path": None,
            "has_full_text": True,
            "arxiv_id": None,
            "acl_id": None,
        },
    )

    dest_path = Path(dest_dir) / f"{paper.id}.pdf"
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(file_bytes)

    corpus.enrich_paper(session, paper.id, {"pdf_path": str(dest_path)})

    return {"id": paper.id}
