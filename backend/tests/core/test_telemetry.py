"""O2 stage telemetry — the per-stage report table in priority_fix.md §4.2.

Counts and elapsed times only: §4.1 forbids logging sentence/paragraph text,
abstracts or credentials, so every assertion here checks a number reached the
log, never a fragment of a paper.
"""

import logging
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.clustering import storage as clustering_storage  # noqa: F401
from app.clustering.schema import GapStatement, StatementCluster
from app.explanation import storage as explanation_storage  # noqa: F401
from app.extraction.schema import FactType
from app.extraction.service import extract_all_parsed, extract_sections
from app.ingestion.corpus import Base, Paper
from app.ner_re.schema import EntityMention, JointExtractionResult, RelationMention
from app.ner_re.service import extract_all
from app.parsing import service as parsing
from app.parsing.section_classifier import get_heading_classifier
from app.parsing.storage import upsert_parsed_document

REQUIRED_ENV = {
    "NEO4J_URI": "bolt://neo4j:7687",
    "NEO4J_USER": "neo4j",
    "NEO4J_PASSWORD": "pw",
    "QDRANT_URL": "http://qdrant:6333",
    "POSTGRES_HOST": "postgres",
    "POSTGRES_PORT": "5432",
    "POSTGRES_USER": "scholarlens",
    "POSTGRES_PASSWORD": "pw",
    "POSTGRES_DB": "scholarlens",
    "PDF_STORAGE_PATH": "/data/pdfs",
    "MAX_UPLOAD_SIZE_MB": "50",
    "UNPAYWALL_EMAIL": "you@example.com",
    "GROBID_URL": "http://grobid:8070",
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch):
    """Several stage services read their caps from the central Settings module."""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def info_logs(caplog):
    caplog.set_level(logging.INFO)
    return caplog


def messages(caplog) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


def test_parse_stage_reports_start_and_completion_counts(
    session, tmp_path, monkeypatch, info_logs
):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    session.add(
        Paper(id=1, source="manual_upload", has_full_text=True, pdf_path=str(pdf))
    )
    session.add(Paper(id=2, source="arxiv", has_full_text=True, pdf_path=None))
    session.commit()

    def fake_parse(session_, paper, grobid_url):
        return upsert_parsed_document(session_, paper.id, [], "fallback")

    monkeypatch.setattr(parsing, "parse_and_store", fake_parse)
    monkeypatch.setattr(
        parsing.header_backfill, "backfill_metadata", Mock(return_value=None)
    )

    parsing.parse_all_pending(session, "http://grobid", "a@b.c", str(tmp_path))

    text = messages(info_logs)
    assert "s2 start papers=2" in text
    assert "s2 complete" in text
    assert "processed=1" in text
    assert "fallback=1" in text
    assert "missing_pdfs=1" in text
    assert "backfill_attempted=1" in text
    assert "backfill_succeeded=0" in text


def test_extract_sections_counts_sentences_rules_and_semantic_calls():
    sections = [
        {
            "type": "Introduction",
            "heading": "Introduction",
            "paragraphs": [
                {"sentences": ["However, the model fails on X.", "We ran it."]}
            ],
        }
    ]
    classifier = Mock()
    classifier.classify_many.return_value = [None]
    stats: dict[str, int] = {}

    extract_sections(sections, classifier, stats=stats)

    assert stats["sentences"] == 2
    assert stats["rule_hits"] == 1
    assert stats["semantic_calls"] == 1


def test_extraction_stage_reports_counts_and_facts_by_type(session, info_logs):
    session.add(Paper(id=1, source="arxiv"))
    session.flush()
    upsert_parsed_document(
        session,
        1,
        [
            {
                "type": "Limitations",
                "heading": "Limitations",
                "paragraphs": [{"sentences": ["However, the model fails on X."]}],
            }
        ],
        "grobid",
    )
    session.commit()
    classifier = Mock()
    classifier.classify_many.return_value = []

    extract_all_parsed(session, classifier)

    text = messages(info_logs)
    assert "s3 start parsed_documents=1" in text
    assert "s3 complete" in text
    assert "sentences=1" in text
    assert "rule_hits=1" in text
    assert f"{FactType.LIMITATION.value}=1" in text


def test_graph_stage_reports_entity_and_relation_counts(session, info_logs):
    session.add(Paper(id=1, source="arxiv", abstract="BERT is trained on SQuAD."))
    session.add(Paper(id=2, source="arxiv", abstract=None))
    session.commit()
    extractor = Mock()
    extractor.extract_many.return_value = JointExtractionResult(
        entities=[
            EntityMention(text="BERT", entity_type="model", confidence=0.9),
            EntityMention(text="SQuAD", entity_type="dataset", confidence=0.9),
        ],
        relations=[
            RelationMention(
                head="BERT", tail="SQuAD", relation_type="trained_on", confidence=0.9
            )
        ],
    )
    neo4j = Mock()
    neo4j.execute_write.return_value = "node-1"

    extract_all(session, neo4j, extractor, max_chars_per_chunk=1200, max_chunks=12)

    text = messages(info_logs)
    assert "s4 start papers=2" in text
    assert "s4 complete" in text
    assert "processed=1" in text
    assert "skipped_no_text=1" in text
    assert "entities=2" in text
    assert "relations=1" in text


def test_index_stage_reports_why_papers_were_skipped(session, info_logs):
    from app.embeddings import indexer

    session.add(Paper(id=1, source="arxiv"))
    session.add(Paper(id=2, source="arxiv"))
    session.flush()
    upsert_parsed_document(session, 2, [], "grobid")
    session.commit()
    index = Mock()
    index.upsert.return_value = 0
    neo4j = Mock()
    neo4j.run.return_value.single.return_value = None

    indexer.index_all(session, neo4j, index)

    text = messages(info_logs)
    assert "s5 start papers=2" in text
    assert "s5 complete" in text
    assert "skipped_no_document=1" in text
    assert "skipped_no_graph_node=1" in text


def test_heading_classifier_counts_keyword_and_semantic_decisions():
    classifier = get_heading_classifier()
    before = (classifier.keyword_count, classifier.semantic_count)
    classifier.embed_fn = lambda texts: [[1.0, 0.0] for _ in texts]
    classifier.references = [[1.0, 0.0] for _ in range(7)]

    classifier.classify_many(["Introduction", "Anecdotal Remarks"])

    assert classifier.keyword_count - before[0] == 1
    assert classifier.semantic_count - before[1] == 1


def test_clustering_stage_reports_hdbscan_and_nli_telemetry(session, info_logs):
    from app.clustering import service as clustering

    statements = [
        GapStatement(
            point_id=f"p{i}",
            paper_id=i,
            # A real-looking limitation: this test is about telemetry, and
            # S6's input filter (D-06-13) drops placeholder text as
            # `too_short` before clustering ever sees it.
            text=(
                f"Evaluation {i} is limited to English prompts and "
                f"accuracy degrades sharply on low-resource languages."
            ),
            year=2024,
            source_type="limitation",
            vector=[1.0, 0.0],
        )
        for i in range(3)
    ]
    index = Mock()
    index.gap_points.return_value = [
        {
            "id": s.point_id,
            "paper_id": s.paper_id,
            "text": s.text,
            "year": s.year,
            "source_type": s.source_type,
            "vector": s.vector,
        }
        for s in statements
    ]
    detector = Mock()
    detector.pairs_scored = 0

    def detect(members):
        detector.pairs_scored += 4
        return []

    detector.detect.side_effect = detect
    monkey_cluster = Mock(
        return_value=[
            StatementCluster(label="theme", members=statements, contradictions=[])
        ]
    )
    original = clustering.cluster_statements
    clustering.cluster_statements = monkey_cluster
    try:
        clustering.rebuild_clusters(session, index=index, detector=detector)
    finally:
        clustering.cluster_statements = original

    text = messages(info_logs)
    assert "s6 gap_points=3" in text
    assert "s6 hdbscan" in text
    assert "noise=0" in text
    assert "nli_pairs_scored=4" in text
