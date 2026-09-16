"""Real SQLite + local Qdrant; only model inference, Neo4j and Groq are mocked."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api import routes_backend
from app.core.config import Settings
from app.core.pipeline import PaperStageState, PipelineRun, pipeline_run, settings_fingerprint
from app.embeddings.encoder import Specter2Encoder
from app.embeddings.indexer import EvidenceIndex
from app.extraction.semantic import SemanticStatementClassifier
from app.ingestion.corpus import Base, Paper
from app.ner_re.schema import JointExtractionResult
from app.parsing.storage import upsert_parsed_document
from tests.api.test_routes_backend import REQUIRED_ENV


@pytest.fixture
def environment(tmp_path, monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    session = Session(engine)
    for i in (1, 2):
        pdf = tmp_path / f'{i}.pdf'
        pdf.write_bytes(f'pdf-{i}'.encode())
        session.add(Paper(id=i, source='arxiv', title=f'Paper {i}', pdf_path=str(pdf), has_full_text=True, year=2024))
    session.commit()
    graph_hashes = {}
    @contextmanager
    def graph_session(*args):
        yield SimpleNamespace()
    monkeypatch.setattr(routes_backend, 'graph_session', graph_session)
    from app.parsing import service as parsing
    from app.extraction import service as extraction
    from app.ner_re import service as nerre
    from app.embeddings import indexer
    from app.clustering import service as clustering
    def parse(s, p, *args):
        return upsert_parsed_document(s, p.id, [{'type': 'Limitations', 'heading': 'Limitations', 'paragraphs': [{'text': 'Our model fails in low-resource settings.'}]}], 'grobid')
    monkeypatch.setattr(parsing, 'run_parsing_pipeline', parse)
    monkeypatch.setattr(extraction, 'get_semantic_classifier', lambda: SemanticStatementClassifier(lambda texts: [[1., 0.] for _ in texts]))
    extractor = Mock()
    extractor.extract_many.return_value = JointExtractionResult([], [])
    monkeypatch.setattr(nerre, 'get_joint_extractor', lambda: extractor)
    monkeypatch.setattr(nerre, 'ensure_graph_schema', lambda graph: None)
    monkeypatch.setattr(nerre, 'write_paper_graph', lambda graph, paper, result, input_hash=None: graph_hashes.update({paper.id: input_hash}))
    monkeypatch.setattr(nerre, 'paper_graph_current', lambda graph, pid, digest: graph_hashes.get(pid) == digest)
    monkeypatch.setattr(indexer, 'get_paper_node_id', lambda graph, pid: f'node-{pid}' if pid in graph_hashes else None)
    embed = Mock(side_effect=lambda texts: [[1., .5, .2] for _ in texts])
    index = EvidenceIndex(settings, QdrantClient(':memory:'), Specter2Encoder(embed, 3))
    monkeypatch.setattr(routes_backend, 'EvidenceIndex', lambda settings: index)
    monkeypatch.setattr(clustering, 'EvidenceIndex', lambda settings: index)
    yield session, settings, index, embed, extractor
    session.close()
    index.client.close()


def execute(session, settings):
    with pipeline_run(session, settings_fingerprint(settings, ('embedding_',))) as run:
        return routes_backend._execute_pipeline(routes_backend.PipelineRequest(generate_reports=False), session, settings, run)


def test_complete_run_then_noop_reuses_models_and_fact_ids(environment):
    session, settings, index, embed, extractor = environment
    first = execute(session, settings)
    assert first['parsed'] == [1, 2]
    assert first['indexed'] > 0
    assert index.has_evidence()
    old_payloads = index.paper_payloads(1)
    embed.reset_mock()
    extractor.reset_mock()
    second = execute(session, settings)
    assert second['parsed'] == second['extracted'] == second['graphed'] == []
    assert second['indexed'] == 0
    embed.assert_not_called()
    extractor.extract_many.assert_not_called()
    assert index.paper_payloads(1) == old_payloads
    assert all(s.status == 'done' for s in session.scalars(select(PaperStageState)))


def test_crash_after_qdrant_upsert_resumes_without_reembedding(environment, monkeypatch):
    session, settings, index, embed, extractor = environment
    prune = index.prune_paper
    failed = False
    def kill(paper_id, keep_ids):
        nonlocal failed
        if not failed:
            failed = True
            raise KeyboardInterrupt('simulated process interruption')
        return prune(paper_id, keep_ids)
    monkeypatch.setattr(index, 'prune_paper', kill)
    with pytest.raises(KeyboardInterrupt):
        execute(session, settings)
    assert session.get(PaperStageState, (1, 's5')).status == 'failed'
    assert session.scalar(select(PipelineRun)).status == 'failed'
    assert index.paper_payloads(1)
    embed.reset_mock()
    execute(session, settings)
    # Paper 1's durable vectors are reused, only the not-yet-indexed paper 2 embeds.
    assert embed.call_count == 1
    assert all(s.status == 'done' for s in session.scalars(select(PaperStageState)))


def test_metadata_only_change_refreshes_payloads(environment):
    session, settings, index, embed, extractor = environment
    execute(session, settings)
    session.get(Paper, 1).year = 2025
    session.commit()
    embed.reset_mock()
    execute(session, settings)
    embed.assert_not_called()
    assert all(p['year'] == 2025 for p in index.paper_payloads(1).values())
