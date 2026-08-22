"""Tests for backend/app/ingestion/manual_upload.py (D-18, INGEST-07)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ingestion.corpus import Base, Paper
from app.ingestion.manual_upload import UploadRejected, save_upload


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_save_upload_writes_file_and_corpus_row(tmp_path, session: Session) -> None:
    result = save_upload(
        file_bytes=b"%PDF-1.4 fake content",
        filename="paper.pdf",
        content_type="application/pdf",
        dest_dir=str(tmp_path),
        max_bytes=10_000_000,
        session=session,
    )

    paper_id = result["id"]
    paper = session.get(Paper, paper_id)
    assert paper.source == "manual_upload"
    assert paper.has_full_text is True
    assert paper.title is None
    assert paper.authors is None
    assert paper.year is None
    assert paper.venue is None
    assert paper.doi is None
    assert paper.abstract is None
    assert paper.arxiv_id is None
    assert paper.acl_id is None

    written_path = tmp_path / f"{paper_id}.pdf"
    assert written_path.read_bytes() == b"%PDF-1.4 fake content"
    assert paper.pdf_path == str(written_path)


def test_save_upload_rejects_non_pdf_content_type(tmp_path, session: Session) -> None:
    with pytest.raises(UploadRejected) as exc_info:
        save_upload(
            file_bytes=b"not a pdf",
            filename="image.png",
            content_type="image/png",
            dest_dir=str(tmp_path),
            max_bytes=10_000_000,
            session=session,
        )

    assert exc_info.value.status_code == 415
    assert list(tmp_path.iterdir()) == []
    assert session.query(Paper).count() == 0


def test_save_upload_rejects_oversized_file(tmp_path, session: Session) -> None:
    with pytest.raises(UploadRejected) as exc_info:
        save_upload(
            file_bytes=b"x" * 100,
            filename="paper.pdf",
            content_type="application/pdf",
            dest_dir=str(tmp_path),
            max_bytes=50,
            session=session,
        )

    assert exc_info.value.status_code == 413
    assert list(tmp_path.iterdir()) == []
    assert session.query(Paper).count() == 0


def test_save_upload_never_uses_client_filename_for_disk_path(
    tmp_path, session: Session
) -> None:
    result = save_upload(
        file_bytes=b"%PDF-1.4 fake content",
        filename="../../etc/passwd.pdf",
        content_type="application/pdf",
        dest_dir=str(tmp_path),
        max_bytes=10_000_000,
        session=session,
    )

    paper_id = result["id"]
    written_path = tmp_path / f"{paper_id}.pdf"
    assert written_path.exists()
    assert ".." not in str(written_path.resolve()).replace(str(tmp_path.resolve()), "")
