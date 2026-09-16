from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.maintenance import delete_paper, resume_deletions
from app.core.pipeline import PaperStageState, pipeline_run
from app.ingestion.corpus import Base, Paper
from app.parsing.storage import upsert_parsed_document, get_parsed_document


def test_deletion_resumes_after_external_failure_and_keeps_pdf(tmp_path):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'keep-original')
    with Session(engine) as session:
        session.add(Paper(id=1, source='arxiv', pdf_path=str(pdf)))
        session.flush()
        upsert_parsed_document(session, 1, [], 'grobid')
        session.commit()
        graph = Mock()
        graph.execute_write.side_effect = RuntimeError('offline')
        index = Mock()
        with pytest.raises(RuntimeError):
            with pipeline_run(session, {}):
                delete_paper(session, graph, index, 1)
        assert session.get(Paper, 1) is not None
        assert session.get(PaperStageState, (1, 'delete')).status == 'tombstone'
        graph.execute_write.side_effect = None
        with pipeline_run(session, {}):
            assert resume_deletions(session, graph, index) == [1]
        assert session.get(Paper, 1) is None
        assert get_parsed_document(session, 1) is None
        assert session.get(PaperStageState, (1, 'delete')) is None
        assert pdf.read_bytes() == b'keep-original'
        assert index.prune_paper.call_count == 2
