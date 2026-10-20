import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.pipeline import pipeline_run
from app.ingestion import corpus
from app.parsing import service, storage
from app.parsing.grobid_client import GrobidOutcome

FULLTEXT_FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "grobid_sample_fulltext.tei.xml"
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    corpus.Base.metadata.create_all(engine)
    with Session(engine) as active:
        yield active


def _grobid_returns(monkeypatch, tei, status=None):
    """Patch GROBID's full-text call. `None` alone means it rejected the PDF."""
    outcome = GrobidOutcome(tei, status or ("ok" if tei is not None else "rejected"))
    fake = Mock(return_value=outcome)
    monkeypatch.setattr(service.grobid_client, "fulltext", fake)
    return fake


def _headings_only(monkeypatch):
    monkeypatch.setattr(
        service.section_classifier,
        "classify_sections",
        lambda headings: ["Introduction"] * len(headings),
    )


# GROBID's answer for a scanned or image-only PDF: section heads, no text.
HEADS_WITHOUT_TEXT_TEI = (
    '<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body>'
    "<div><head>1 Introduction</head></div>"
    "<div><head>2 Method</head><p> </p></div>"
    "</body></text></TEI>"
)
FALLBACK_WITH_TEXT = [
    {"heading": None, "type": None, "paragraphs": [{"text": "Recovered body."}]}
]


def test_grobid_structure_without_text_falls_back(session, tmp_path, monkeypatch):
    """Headings alone are not a parse: the fallback must get its chance."""
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    _grobid_returns(monkeypatch, HEADS_WITHOUT_TEXT_TEI)
    fallback = Mock(return_value=FALLBACK_WITH_TEXT)
    monkeypatch.setattr(service.fallback, "extract_fallback_sections", fallback)
    _headings_only(monkeypatch)

    document = service.parse_and_store(
        session, _paper(session, "arxiv", str(pdf)), "url"
    )

    fallback.assert_called_once_with(b"pdf")
    assert document.parser_used == "fallback"
    assert document.sections[0]["paragraphs"][0]["text"] == "Recovered body."


def test_no_text_from_any_parser_is_recorded_as_none(session, tmp_path, monkeypatch):
    """A PDF neither parser can read is a visible failure, not a fallback parse."""
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    _grobid_returns(monkeypatch, HEADS_WITHOUT_TEXT_TEI)
    monkeypatch.setattr(
        service.fallback, "extract_fallback_sections", Mock(return_value=None)
    )
    _headings_only(monkeypatch)

    document = service.parse_and_store(
        session, _paper(session, "arxiv", str(pdf)), "url"
    )

    assert document.parser_used == "none"
    # No text was recovered, so no structure is stored as if it were evidence.
    assert document.sections == []


def test_fallback_while_grobid_unavailable_is_marked_provisional(
    session, tmp_path, monkeypatch
):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    _grobid_returns(monkeypatch, None, "unavailable")
    monkeypatch.setattr(
        service.fallback,
        "extract_fallback_sections",
        Mock(return_value=FALLBACK_WITH_TEXT),
    )
    _headings_only(monkeypatch)

    document = service.parse_and_store(
        session, _paper(session, "arxiv", str(pdf)), "url"
    )

    assert document.parser_used == "fallback_grobid_unavailable"


def test_checkpointed_s2_retries_grobid_only_for_provisional_fallbacks(
    session, tmp_path, monkeypatch
):
    """A GROBID outage must not permanently downgrade a paper to the fallback.

    The PDF bytes do not change, so the input hash alone would call the
    outage-time parse current forever. A document GROBID itself rejected is
    final, though: re-sending it every run would only repeat the same 500.
    """
    rejected_pdf, outage_pdf = tmp_path / "a.pdf", tmp_path / "b.pdf"
    rejected_pdf.write_bytes(b"rejected")
    outage_pdf.write_bytes(b"outage")
    rejected = _paper(session, "arxiv", str(rejected_pdf))
    outage = _paper(session, "arxiv", str(outage_pdf))
    session.commit()
    monkeypatch.setattr(
        service.fallback,
        "extract_fallback_sections",
        Mock(return_value=FALLBACK_WITH_TEXT),
    )
    _headings_only(monkeypatch)
    xml = FULLTEXT_FIXTURE.read_text(encoding="utf-8")

    monkeypatch.setattr(
        service.grobid_client,
        "fulltext",
        lambda pdf, url: GrobidOutcome(
            None, "rejected" if pdf == b"rejected" else "unavailable"
        ),
    )
    with pipeline_run(session, {}):
        first = service.parse_all_pending(
            session, "url", "e@example.org", str(tmp_path)
        )
    assert first.processed == [rejected.id, outage.id]
    assert first.grobid_unavailable == [outage.id]

    grobid_back = _grobid_returns(monkeypatch, xml)
    with pipeline_run(session, {}):
        second = service.parse_all_pending(
            session, "url", "e@example.org", str(tmp_path)
        )

    assert second.processed == [outage.id]
    grobid_back.assert_called_once_with(b"outage", "url")
    assert storage.get_parsed_document(session, outage.id).parser_used == "grobid"
    assert storage.get_parsed_document(session, rejected.id).parser_used == "fallback"


def test_parse_batch_reports_documents_with_no_text(session, tmp_path, monkeypatch):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    paper = _paper(session, "arxiv", str(pdf))
    session.commit()
    _grobid_returns(monkeypatch, None)
    monkeypatch.setattr(
        service.fallback, "extract_fallback_sections", Mock(return_value=None)
    )
    _headings_only(monkeypatch)

    with pipeline_run(session, {}):
        batch = service.parse_all_pending(
            session, "url", "e@example.org", str(tmp_path)
        )

    assert batch.processed == [paper.id]
    assert batch.no_text == [paper.id]


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
    _grobid_returns(monkeypatch, xml)
    monkeypatch.setattr(
        service.section_classifier,
        "classify_sections",
        lambda headings: ["Introduction"] * len(headings),
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
    raw = [
        {
            "heading": "Conclusion",
            "type": None,
            "paragraphs": [{"text": "We conclude."}],
        }
    ]
    _grobid_returns(monkeypatch, None)
    fallback = Mock(return_value=raw)
    monkeypatch.setattr(service.fallback, "extract_fallback_sections", fallback)
    monkeypatch.setattr(
        service.section_classifier,
        "classify_sections",
        lambda headings: ["Conclusion"] * len(headings),
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
    _grobid_returns(monkeypatch, "<not-closed")
    fallback = Mock(return_value=FALLBACK_WITH_TEXT)
    monkeypatch.setattr(service.fallback, "extract_fallback_sections", fallback)
    monkeypatch.setattr(
        service.section_classifier,
        "classify_sections",
        lambda headings: ["Introduction"] * len(headings),
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
    _grobid_returns(monkeypatch, empty_tei)
    fallback = Mock(return_value=FALLBACK_WITH_TEXT)
    monkeypatch.setattr(service.fallback, "extract_fallback_sections", fallback)
    monkeypatch.setattr(
        service.section_classifier,
        "classify_sections",
        lambda headings: ["Introduction"] * len(headings),
    )

    document = service.parse_and_store(
        session, _paper(session, "acl_anthology", str(pdf)), "url"
    )

    assert document.parser_used == "fallback"
    fallback.assert_called_once_with(b"pdf")


@pytest.mark.parametrize(
    ("tei", "reason"),
    [
        (None, "no GROBID response"),
        ("<not-closed", "unparseable TEI"),
        (
            '<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body /></text></TEI>',
            "no body sections",
        ),
    ],
)
def test_fallback_is_logged_with_paper_and_reason(
    session, tmp_path, monkeypatch, caplog, tei, reason
) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    _grobid_returns(monkeypatch, tei)
    monkeypatch.setattr(
        service.fallback,
        "extract_fallback_sections",
        Mock(
            return_value=[
                {"heading": None, "type": None, "paragraphs": [{"text": "Body."}]}
            ]
        ),
    )
    monkeypatch.setattr(
        service.section_classifier,
        "classify_sections",
        lambda headings: ["Introduction"] * len(headings),
    )
    paper = _paper(session, "arxiv", str(pdf))

    with caplog.at_level("WARNING", logger="app.parsing.service"):
        service.parse_and_store(session, paper, "url")

    assert f"paper={paper.id}" in caplog.text
    assert reason in caplog.text
    assert "no text" not in caplog.text


def test_document_with_no_text_is_logged(session, tmp_path, monkeypatch, caplog):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    _grobid_returns(monkeypatch, None)
    monkeypatch.setattr(
        service.fallback, "extract_fallback_sections", Mock(return_value=None)
    )
    monkeypatch.setattr(
        service.section_classifier, "classify_sections", lambda headings: []
    )
    paper = _paper(session, "arxiv", str(pdf))

    with caplog.at_level("WARNING", logger="app.parsing.service"):
        document = service.parse_and_store(session, paper, "url")

    assert document.sections == []
    assert f"paper={paper.id}" in caplog.text
    assert "no text" in caplog.text


def test_parse_and_store_classifies_grobid_sections(
    session, tmp_path, monkeypatch
) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    xml = FULLTEXT_FIXTURE.read_text(encoding="utf-8")
    _grobid_returns(monkeypatch, xml)
    monkeypatch.setattr(
        service.section_classifier,
        "classify_sections",
        lambda headings: ["Discussion"] * len(headings),
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


def test_parse_all_pending_retries_backfill_for_parsed_untitled_manual_uploads(
    session, tmp_path, monkeypatch
) -> None:
    """A manual upload parsed while GROBID was down must not stay untitled forever.

    GROBID failure makes `parse_and_store` fall back and still store a document,
    and the header backfill silently no-ops. Skipping every already-parsed paper
    then leaves metadata GROBID can recover unrecovered on every later run
    (README §15 Phase 2 "Done when").
    """
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    untitled_upload = _paper(session, "manual_upload", str(pdf), title=None)
    titled_upload = _paper(session, "manual_upload", str(pdf))
    untitled_arxiv = _paper(session, "arxiv", str(pdf), title=None)
    for paper in (untitled_upload, titled_upload, untitled_arxiv):
        storage.upsert_parsed_document(session, paper.id, [], "fallback")
    run = Mock()
    backfill = Mock()
    monkeypatch.setattr(service, "run_parsing_pipeline", run)
    monkeypatch.setattr(service.header_backfill, "backfill_metadata", backfill)

    result = service.parse_all_pending(session, "url", "email", "pdfs")

    assert run.call_count == 0
    assert result.processed == []
    backfilled = [call.args[1].id for call in backfill.call_args_list]
    assert backfilled == [untitled_upload.id]
