from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.parsing import header_backfill
from app.parsing.header_backfill import backfill_metadata, extract_header_fields

FIXTURE = Path(__file__).parents[1] / "fixtures" / "grobid_sample_header.tei.xml"


def _with_doi(xml: str) -> str:
    return xml.replace(
        '<idno type="MD5">',
        '<idno type="DOI">10.1234/test</idno><idno type="MD5">',
        1,
    )


def test_extract_header_fields_handles_absent_doi() -> None:
    fields = extract_header_fields(FIXTURE.read_text(encoding="utf-8"))
    assert fields["title"] == "Attention Is All You Need"
    assert len(fields["authors"]) >= 9
    assert fields["authors"][0] == "Ashish Vaswani"
    assert fields["doi"] is None


def test_extract_header_fields_reads_doi() -> None:
    fields = extract_header_fields(_with_doi(FIXTURE.read_text(encoding="utf-8")))
    assert fields["doi"] == "10.1234/test"


def test_backfill_without_doi_updates_metadata_only(tmp_path, monkeypatch) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    paper = SimpleNamespace(id=7, pdf_path=str(pdf))
    xml = FIXTURE.read_text(encoding="utf-8")
    enrich_paper = Mock(return_value=paper)
    enrich_oa = Mock()
    monkeypatch.setattr(
        header_backfill.grobid_client, "parse_header", Mock(return_value=xml)
    )
    monkeypatch.setattr(header_backfill.corpus, "enrich_paper", enrich_paper)
    monkeypatch.setattr(header_backfill.unpaywall, "enrich", enrich_oa)

    assert (
        backfill_metadata(Mock(), paper, "http://grobid", "a@b.test", str(tmp_path))
        is paper
    )
    updates = enrich_paper.call_args.args[2]
    assert updates["title"] == "Attention Is All You Need"
    assert "doi" not in updates
    enrich_oa.assert_not_called()


def test_backfill_with_doi_reuses_unpaywall(tmp_path, monkeypatch) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    paper = SimpleNamespace(id=8, pdf_path=str(pdf))
    xml = _with_doi(FIXTURE.read_text(encoding="utf-8"))
    session = Mock()
    enrich_paper = Mock(return_value=paper)
    enrich_oa = Mock()
    monkeypatch.setattr(
        header_backfill.grobid_client, "parse_header", Mock(return_value=xml)
    )
    monkeypatch.setattr(header_backfill.corpus, "enrich_paper", enrich_paper)
    monkeypatch.setattr(header_backfill.unpaywall, "enrich", enrich_oa)

    backfill_metadata(session, paper, "http://grobid", "a@b.test", str(tmp_path))

    assert enrich_paper.call_args.args[2]["doi"] == "10.1234/test"
    enrich_oa.assert_called_once_with(session, "a@b.test", str(tmp_path))


def test_backfill_is_noop_when_header_parse_fails(tmp_path, monkeypatch) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    paper = SimpleNamespace(id=9, pdf_path=str(pdf))
    enrich_paper = Mock()
    enrich_oa = Mock()
    monkeypatch.setattr(
        header_backfill.grobid_client, "parse_header", Mock(return_value=None)
    )
    monkeypatch.setattr(header_backfill.corpus, "enrich_paper", enrich_paper)
    monkeypatch.setattr(header_backfill.unpaywall, "enrich", enrich_oa)

    assert (
        backfill_metadata(Mock(), paper, "http://grobid", "a@b.test", str(tmp_path))
        is None
    )
    enrich_paper.assert_not_called()
    enrich_oa.assert_not_called()
