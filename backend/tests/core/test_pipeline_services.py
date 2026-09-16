from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.pipeline import PaperStageState, pipeline_run
from app.ingestion.corpus import Base, Paper
from app.parsing import service as parsing
from app.parsing.storage import upsert_parsed_document
from app.extraction import service as extraction


@pytest.fixture
def session():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_changed_pdf_reparsed_unchanged_skipped(session, tmp_path, monkeypatch):
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'original')
    session.add(Paper(id=1, source='arxiv', has_full_text=True, pdf_path=str(pdf)))
    session.commit()
    parse = Mock(side_effect=lambda s, p, *args: upsert_parsed_document(s, p.id, [], 'grobid'))
    monkeypatch.setattr(parsing, 'run_parsing_pipeline', parse)
    for _ in range(2):
        with pipeline_run(session, {}):
            parsing.parse_all_pending(session, 'grobid', 'contact', str(tmp_path))
    assert parse.call_count == 1
    pdf.write_bytes(b'updated')
    with pipeline_run(session, {}):
        parsing.parse_all_pending(session, 'grobid', 'contact', str(tmp_path))
    assert parse.call_count == 2


def test_extraction_checkpoint_skips_same_sections(session, monkeypatch):
    session.add(Paper(id=1, source='arxiv'))
    session.flush()
    doc = upsert_parsed_document(session, 1, [], 'grobid')
    session.commit()
    extract = Mock()
    monkeypatch.setattr(extraction, 'extract_and_store', extract)
    for _ in range(2):
        with pipeline_run(session, {}):
            extraction.extract_all_parsed(session, Mock())
    assert extract.call_count == 1
    doc.sections = [{'heading': 'Updated'}]
    session.commit()
    with pipeline_run(session, {}):
        extraction.extract_all_parsed(session, Mock())
    assert extract.call_count == 2
    assert session.get(PaperStageState, (1, 's3')).status == 'done'


def test_failed_extraction_is_not_marked_complete(session, monkeypatch):
    session.add(Paper(id=1, source='arxiv'))
    session.flush()
    upsert_parsed_document(session, 1, [], 'grobid')
    session.commit()
    monkeypatch.setattr(extraction, 'extract_and_store', Mock(side_effect=RuntimeError()))
    with pytest.raises(RuntimeError):
        with pipeline_run(session, {}):
            extraction.extract_all_parsed(session, Mock())
    assert session.get(PaperStageState, (1, 's3')).status == 'failed'
