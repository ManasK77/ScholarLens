"""Tests for app/ner_re/service.py — Phase 4 orchestration over the corpus.

Runs against an in-memory SQLite corpus and a fake extractor / fake Neo4j
session, so no checkpoint and no graph database are needed.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.extraction.schema import ExtractedStatement, FactType
from app.extraction.storage import replace_facts
from app.ingestion.corpus import Base, Paper
from app.ner_re import service
from app.ner_re.schema import EntityMention, JointExtractionResult, RelationMention

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
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """extract_all() reads its chunk caps from the central Settings module."""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


class FakeNeo4jSession:
    def __init__(self):
        self.queries = []

    def run(self, query, **parameters):
        self.queries.append((query, parameters))

        class _Result:
            def single(self_inner):
                return {"node_id": "neo4j-node-1"}

        return _Result()


class RecordingExtractor:
    """Returns a fixed result and records every chunk it was asked to process."""

    def __init__(self, result=None):
        self.seen_chunks = []
        self._result = result or JointExtractionResult(
            entities=[
                EntityMention("BERT", "model", 0.9),
                EntityMention("SQuAD", "dataset", 0.9),
            ],
            relations=[RelationMention("BERT", "trained_on", "SQuAD", 0.8)],
        )

    def extract_many(self, chunks):
        chunks = list(chunks)
        self.seen_chunks.append(chunks)
        return self._result


def _add_paper(session, **overrides):
    paper = Paper(
        title=overrides.get("title", "A Paper"),
        authors=overrides.get("authors"),
        year=overrides.get("year", 2024),
        venue=overrides.get("venue", "ACL"),
        doi=overrides.get("doi"),
        abstract=overrides.get("abstract"),
        source=overrides.get("source", "arxiv"),
        has_full_text=True,
    )
    session.add(paper)
    session.flush()
    return paper


def _add_facts(session, paper_id, *texts, fact_type=FactType.MODEL):
    """replace_facts() replaces every row for the paper, so write them at once."""
    replace_facts(
        session,
        paper_id,
        [
            ExtractedStatement(
                fact_type=fact_type,
                text=text,
                section_type="methodology",
                section_heading="Method",
                section_index=0,
                paragraph_index=0,
                sentence_index=index,
                confidence=1.0,
                extraction_method="rule",
            )
            for index, text in enumerate(texts)
        ],
    )


# --------------------------------------------------------------------------
# Text selection
# --------------------------------------------------------------------------


def test_collect_paper_text_uses_abstract_and_extracted_facts(session) -> None:
    paper = _add_paper(session, abstract="We introduce BERT.")
    _add_facts(session, paper.id, "BERT is trained on SQuAD.")

    chunks = service.collect_paper_text(session, paper)

    joined = " ".join(chunks)
    assert "We introduce BERT." in joined
    assert "BERT is trained on SQuAD." in joined


def test_collect_paper_text_deduplicates_repeated_sentences(session) -> None:
    paper = _add_paper(session, abstract="BERT is trained on SQuAD.")
    _add_facts(session, paper.id, "BERT is trained on SQuAD.")

    chunks = service.collect_paper_text(session, paper)

    assert " ".join(chunks).count("BERT is trained on SQuAD.") == 1


def test_collect_paper_text_is_empty_when_nothing_is_available(session) -> None:
    paper = _add_paper(session, abstract=None)

    assert service.collect_paper_text(session, paper) == []


def test_collect_paper_text_respects_the_chunk_character_budget(session) -> None:
    paper = _add_paper(session, abstract=None)
    _add_facts(
        session,
        paper.id,
        *[f"Sentence number {index} about BERT and SQuAD." for index in range(12)],
    )

    chunks = service.collect_paper_text(session, paper, max_chars_per_chunk=80)

    assert len(chunks) > 1
    assert all(len(chunk) <= 80 for chunk in chunks), [len(c) for c in chunks]


def test_collect_paper_text_caps_the_number_of_chunks(session) -> None:
    paper = _add_paper(session, abstract=None)
    _add_facts(
        session,
        paper.id,
        *[f"Sentence {index} about BERT and SQuAD here." for index in range(60)],
    )

    chunks = service.collect_paper_text(
        session, paper, max_chars_per_chunk=60, max_chunks=3
    )

    assert len(chunks) == 3


def test_a_sentence_longer_than_the_budget_still_produces_a_chunk(session) -> None:
    paper = _add_paper(session, abstract="x" * 500)

    chunks = service.collect_paper_text(session, paper, max_chars_per_chunk=100)

    assert chunks, "an over-long sentence must not silently vanish"


# --------------------------------------------------------------------------
# extract_all
# --------------------------------------------------------------------------


def test_extract_all_processes_papers_and_returns_their_ids(session) -> None:
    first = _add_paper(session, abstract="We introduce BERT.")
    second = _add_paper(session, abstract="We evaluate GPT-4.")
    extractor = RecordingExtractor()
    neo4j = FakeNeo4jSession()

    processed = service.extract_all(session, neo4j, extractor=extractor)

    assert processed == [first.id, second.id]


def test_extract_all_signature_matches_the_two_argument_route_call(session) -> None:
    """routes_backend.py calls graph_service.extract_all(session, neo4j)."""
    _add_paper(session, abstract="We introduce BERT.")

    processed = service.extract_all(session, FakeNeo4jSession(), RecordingExtractor())

    assert processed


def test_extract_all_writes_entities_and_relations_to_the_graph(session) -> None:
    _add_paper(session, abstract="BERT is trained on SQuAD.")
    neo4j = FakeNeo4jSession()

    service.extract_all(session, neo4j, extractor=RecordingExtractor())

    queries = " ".join(query for query, _ in neo4j.queries)
    assert "MERGE (p:Paper" in queries
    assert "MERGE (e:Entity" in queries
    assert "[r:TRAINED_ON" in queries


def test_extract_all_ensures_the_graph_schema_once(session) -> None:
    _add_paper(session, abstract="One.")
    _add_paper(session, abstract="Two.")
    neo4j = FakeNeo4jSession()

    service.extract_all(session, neo4j, extractor=RecordingExtractor())

    constraints = [q for q, _ in neo4j.queries if "CREATE CONSTRAINT" in q]
    assert len(constraints) == 2, "one paper_id + one entity_key constraint, once"


def test_extract_all_skips_papers_with_no_usable_text(session) -> None:
    empty = _add_paper(session, abstract=None)
    usable = _add_paper(session, abstract="We introduce BERT.")
    extractor = RecordingExtractor()

    processed = service.extract_all(session, FakeNeo4jSession(), extractor=extractor)

    assert empty.id not in processed
    assert usable.id in processed


def test_extract_all_does_not_branch_on_the_source_field(session) -> None:
    """README hard constraint: Phase 2 onward is source-agnostic."""
    arxiv = _add_paper(session, abstract="BERT text.", source="arxiv")
    manual = _add_paper(session, abstract="BERT text.", source="manual_upload")
    extractor = RecordingExtractor()

    processed = service.extract_all(session, FakeNeo4jSession(), extractor=extractor)

    assert processed == [arxiv.id, manual.id]
    assert extractor.seen_chunks[0] == extractor.seen_chunks[1]


def test_extract_all_continues_when_one_paper_fails(session) -> None:
    failing = _add_paper(session, abstract="Boom.")
    healthy = _add_paper(session, abstract="We introduce BERT.")

    class FlakyExtractor(RecordingExtractor):
        def extract_many(self, chunks):
            chunks = list(chunks)
            if any("Boom." in chunk for chunk in chunks):
                raise RuntimeError("checkpoint blew up")
            return super().extract_many(chunks)

    processed = service.extract_all(
        session, FakeNeo4jSession(), extractor=FlakyExtractor()
    )

    assert failing.id not in processed
    assert healthy.id in processed


def test_extract_all_passes_every_chunk_to_one_extract_many_call(session) -> None:
    paper = _add_paper(session, abstract=None)
    _add_facts(
        session,
        paper.id,
        *[f"Sentence {index} about BERT and SQuAD here." for index in range(6)],
    )
    extractor = RecordingExtractor()

    service.extract_all(
        session, FakeNeo4jSession(), extractor=extractor, max_chars_per_chunk=60
    )

    assert len(extractor.seen_chunks) == 1, "one batched call per paper"
    assert len(extractor.seen_chunks[0]) > 1
