import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ingestion import corpus
from app.parsing import service, storage

FULLTEXT_FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "grobid_sample_fulltext.tei.xml"
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    corpus.Base.metadata.create_all(engine)
    with Session(engine) as active:
        yield active


def _paper(session: Session, source: str, pdf_path: str, title="Paper"):
    return corpus.insert_paper(
        session,
        {
            "title": title,
            "authors": [],
            "year": 2024,
            "venue": None,
            "doi": None,
            "abstract": None,
            "source": source,
            "pdf_path": pdf_path,
            "has_full_text": True,
            "arxiv_id": None,
            "acl_id": None,
        },
    )


def test_parse_and_store_is_source_agnostic(session, tmp_path, monkeypatch) -> None:
    pdf = tmp_path / "same.pdf"
    pdf.write_bytes(b"same")
    xml = FULLTEXT_FIXTURE.read_text(encoding="utf-8")
    monkeypatch.setattr(service.grobid_client, "parse_fulltext", Mock(return_value=xml))
    monkeypatch.setattr(
        service.section_classifier, "classify_section", lambda heading: "Introduction"
    )

    outputs = []
    for source in ["arxiv", "semantic_scholar", "acl_anthology", "manual_upload"]:
        outputs.append(
            service.parse_and_store(
                session, _paper(session, source, str(pdf)), "url"
            ).sections
        )

    assert all(output == outputs[0] for output in outputs[1:])
    source = inspect.getsource(service.parse_and_store)
    assert "paper.source" not in source
    assert ".source" not in source


def test_parse_and_store_uses_fallback_on_grobid_failure(
    session, tmp_path, monkeypatch
) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    raw = [{"heading": "Conclusion", "type": None, "paragraphs": []}]
    monkeypatch.setattr(
        service.grobid_client, "parse_fulltext", Mock(return_value=None)
    )
    fallback = Mock(return_value=raw)
    monkeypatch.setattr(service.fallback, "extract_fallback_sections", fallback)
    monkeypatch.setattr(
        service.section_classifier, "classify_section", lambda heading: "Conclusion"
    )

    document = service.parse_and_store(
        session, _paper(session, "arxiv", str(pdf)), "url"
    )

    assert document.parser_used == "fallback"
    assert document.sections[0]["type"] == "Conclusion"
    fallback.assert_called_once_with(b"pdf")


def test_parse_and_store_uses_fallback_on_malformed_tei(
    session, tmp_path, monkeypatch
) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    monkeypatch.setattr(
        service.grobid_client, "parse_fulltext", Mock(return_value="<not-closed")
    )
    fallback = Mock(return_value=[{"heading": None, "type": None, "paragraphs": []}])
    monkeypatch.setattr(service.fallback, "extract_fallback_sections", fallback)
    monkeypatch.setattr(
        service.section_classifier, "classify_section", lambda heading: "Introduction"
    )

    document = service.parse_and_store(
        session, _paper(session, "semantic_scholar", str(pdf)), "url"
    )

    assert document.parser_used == "fallback"
    fallback.assert_called_once_with(b"pdf")


def test_parse_and_store_uses_fallback_when_tei_has_no_body_sections(
    session, tmp_path, monkeypatch
) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    empty_tei = '<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body /></text></TEI>'
    monkeypatch.setattr(
        service.grobid_client, "parse_fulltext", Mock(return_value=empty_tei)
    )
    fallback = Mock(return_value=[{"heading": None, "type": None, "paragraphs": []}])
    monkeypatch.setattr(service.fallback, "extract_fallback_sections", fallback)
    monkeypatch.setattr(
        service.section_classifier, "classify_section", lambda heading: "Introduction"
    )

    document = service.parse_and_store(
        session, _paper(session, "acl_anthology", str(pdf)), "url"
    )

    assert document.parser_used == "fallback"
    fallback.assert_called_once_with(b"pdf")


def test_parse_and_store_classifies_grobid_sections(
    session, tmp_path, monkeypatch
) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    xml = FULLTEXT_FIXTURE.read_text(encoding="utf-8")
    monkeypatch.setattr(service.grobid_client, "parse_fulltext", Mock(return_value=xml))
    monkeypatch.setattr(
        service.section_classifier, "classify_section", lambda heading: "Discussion"
    )

    document = service.parse_and_store(
        session, _paper(session, "arxiv", str(pdf)), "url"
    )

    assert document.parser_used == "grobid"
    assert document.sections
    assert all(section["type"] == "Discussion" for section in document.sections)


@pytest.mark.parametrize(
    ("source", "title", "should_backfill"),
    [
        ("manual_upload", None, True),
        ("manual_upload", "Known", False),
        ("arxiv", None, False),
    ],
)
def test_run_pipeline_backfill_scope(
    monkeypatch, source, title, should_backfill
) -> None:
    document = object()
    parse = Mock(return_value=document)
    backfill = Mock()
    monkeypatch.setattr(service, "parse_and_store", parse)
    monkeypatch.setattr(service.header_backfill, "backfill_metadata", backfill)
    paper = SimpleNamespace(source=source, title=title)
    session = Mock()

    assert (
        service.run_parsing_pipeline(session, paper, "url", "email", "pdfs") is document
    )
    assert backfill.called is should_backfill


def test_parse_all_pending_only_processes_missing_documents(
    session, tmp_path, monkeypatch
) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    done = _paper(session, "arxiv", str(pdf))
    pending = _paper(session, "acl_anthology", str(pdf))
    storage.upsert_parsed_document(session, done.id, [], "grobid")
    run = Mock()
    monkeypatch.setattr(service, "run_parsing_pipeline", run)

    result = service.parse_all_pending(session, "url", "email", "pdfs")

    assert result.processed == [pending.id]
    assert result.missing_pdfs == []
    assert run.call_count == 1
    assert run.call_args.args[1].id == pending.id


def test_parse_all_pending_repairs_stale_pdf_records_and_continues(
    session, tmp_path, monkeypatch
) -> None:
    valid_pdf = tmp_path / "valid.pdf"
    valid_pdf.write_bytes(b"pdf")
    valid = _paper(session, "arxiv", str(valid_pdf))
    missing = _paper(session, "manual_upload", str(tmp_path / "missing.pdf"))
    metadata_only = _paper(session, "semantic_scholar", None)
    metadata_only.has_full_text = False
    run = Mock()
    monkeypatch.setattr(service, "run_parsing_pipeline", run)

    result = service.parse_all_pending(session, "url", "email", "pdfs")

    assert result.processed == [valid.id]
    assert result.missing_pdfs == [missing.id]
    assert missing.has_full_text is False
    assert missing.pdf_path is None
    assert metadata_only.id not in result.missing_pdfs
    assert [call.args[1].id for call in run.call_args_list] == [valid.id]
