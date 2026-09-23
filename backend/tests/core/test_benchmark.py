"""The offline, read-only benchmark harness (priority_fix.md §D step 5).

Everything here runs without a model: the suites take their encoders and
extractors as arguments precisely so the comparison logic can be tested on
fakes, and so the harness itself can be shown to write nothing.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core import benchmark
from app.extraction import storage as extraction_storage  # noqa: F401
from app.extraction.schema import FactType, SemanticMatch
from app.ingestion.corpus import Base, Paper
from app.ner_re.schema import EntityMention, JointExtractionResult, RelationMention
from app.parsing.storage import upsert_parsed_document


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _sections():
    return [
        {
            "type": "Introduction",
            "heading": "Anecdotal Remarks",
            "paragraphs": [
                {
                    # Neither sentence matches a Phase 3 rule, so both reach
                    # the SciBERT path this suite measures.
                    "sentences": [
                        "We ran the system on a laptop.",
                        "It was done in the afternoon.",
                    ]
                }
            ],
        }
    ]


def test_sample_parsed_documents_is_read_only_and_bounded(session):
    for index in range(3):
        session.add(Paper(id=index + 1, source="arxiv"))
        session.flush()
        upsert_parsed_document(session, index + 1, _sections(), "grobid")
    session.commit()

    sampled = benchmark.sample_parsed_documents(session, limit=2)

    assert [document.paper_id for document in sampled] == [1, 2]
    assert not session.dirty and not session.new and not session.deleted


def test_unmatched_sentences_excludes_rule_hits(session):
    sections = [
        {
            "type": "Introduction",
            "heading": "Introduction",
            "paragraphs": [
                {"sentences": ["However, the model fails on X.", "We ran it."]}
            ],
        }
    ]

    assert benchmark.unmatched_sentences(sections) == ["We ran it."]


def test_label_parity_counts_agreements_and_disagreements():
    report = benchmark.label_parity(
        [
            SemanticMatch(FactType.LIMITATION, 0.9),
            None,
            SemanticMatch(FactType.MODEL, 0.8),
        ],
        [
            SemanticMatch(FactType.LIMITATION, 0.9),
            None,
            SemanticMatch(FactType.METRIC, 0.8),
        ],
    )

    assert report["total"] == 3
    assert report["matched"] == 2
    assert report["mismatched"] == 1
    assert report["parity"] == pytest.approx(2 / 3)


def test_label_parity_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        benchmark.label_parity([None], [None, None])


def test_padding_ratio_is_one_when_every_text_is_the_same_length():
    assert benchmark.padding_ratio([4, 4, 4, 4], batch_size=2) == pytest.approx(1.0)


def test_padding_ratio_reports_the_waste_of_document_order():
    # Batches of 2 over [1, 9, 1, 9]: each batch pads to 9, so 36 positions are
    # computed for 20 real tokens. Sorting to [1, 1, 9, 9] pays only 20.
    assert benchmark.padding_ratio([1, 9, 1, 9], batch_size=2) == pytest.approx(36 / 20)
    assert benchmark.padding_ratio(sorted([1, 9, 1, 9]), batch_size=2) == pytest.approx(
        1.0
    )


def test_padding_report_compares_document_order_against_sorted():
    report = benchmark.padding_report([1, 9, 1, 9], batch_size=2)

    assert report["current"] == pytest.approx(36 / 20)
    assert report["sorted"] == pytest.approx(1.0)
    assert report["speedup"] == pytest.approx(1.8)


def test_vector_drift_reports_the_worst_cosine_similarity():
    report = benchmark.vector_drift(
        [[1.0, 0.0], [0.0, 1.0]],
        [[1.0, 0.0], [1.0, 0.0]],
    )

    assert report["min_cosine"] == pytest.approx(0.0)
    assert report["mean_cosine"] == pytest.approx(0.5)
    assert report["count"] == 2


def test_cluster_stability_is_one_for_identical_partitions():
    assert benchmark.cluster_stability([0, 0, 1, 1], [1, 1, 0, 0]) == pytest.approx(1.0)


def test_set_parity_compares_entities_and_relations():
    def result(entity_text, relation_tail):
        return JointExtractionResult(
            entities=[
                EntityMention(text=entity_text, entity_type="model", confidence=0.9)
            ],
            relations=[
                RelationMention(
                    head=entity_text,
                    tail=relation_tail,
                    relation_type="trained_on",
                    confidence=0.9,
                )
            ],
        )

    report = benchmark.set_parity(result("BERT", "SQuAD"), result("BERT", "GLUE"))

    assert report["entities_identical"] is True
    assert report["relations_identical"] is False
    assert report["entity_jaccard"] == pytest.approx(1.0)
    assert report["relation_jaccard"] == pytest.approx(0.0)


def test_extraction_suite_reports_parity_and_timing(session):
    session.add(Paper(id=1, source="arxiv"))
    session.flush()
    upsert_parsed_document(session, 1, _sections(), "grobid")
    session.commit()

    class FakeClassifier:
        def classify(self, sentence):
            return SemanticMatch(FactType.MODEL, 0.9)

        def classify_many(self, sentences, batch_size=32):
            return [SemanticMatch(FactType.MODEL, 0.9) for _ in sentences]

    report = benchmark.run_extraction_suite(session, FakeClassifier(), limit=5)

    assert report["sentences"] == 2
    assert report["parity"]["parity"] == pytest.approx(1.0)
    assert report["single_seconds"] >= 0.0
    assert report["batched_seconds"] >= 0.0


def test_heading_suite_reports_argmax_parity(session, monkeypatch):
    session.add(Paper(id=1, source="arxiv"))
    session.flush()
    upsert_parsed_document(session, 1, _sections(), "grobid")
    session.commit()
    monkeypatch.setattr(
        benchmark.section_classifier,
        "classify_heading",
        lambda heading, embed_fn=None: "Discussion",
    )

    report = benchmark.run_heading_suite(
        session, embed_fn=lambda texts: [[1.0, 0.0] for _ in texts], limit=5
    )

    assert report["headings"] == 1
    assert report["parity"]["total"] == 1


def test_nerre_suite_compares_per_chunk_against_batched(session):
    session.add(Paper(id=1, source="arxiv", abstract="BERT is trained on SQuAD."))
    session.commit()

    result = JointExtractionResult(
        entities=[EntityMention(text="BERT", entity_type="model", confidence=0.9)],
        relations=[],
    )

    class FakeExtractor:
        def __init__(self, batch_size):
            self._batch_size = batch_size
            self.calls = 0

        def extract_many(self, chunks):
            self.calls += 1
            return result

    single, batched = FakeExtractor(1), FakeExtractor(4)
    report = benchmark.run_nerre_suite(session, single, batched, limit=5)

    assert report["papers"] == 1
    assert report["parity"]["entities_identical"] is True
    assert single.calls == 1 and batched.calls == 1


def test_thread_report_describes_the_process_cpu_budget():
    report = benchmark.thread_report()

    assert report["cpu_count"] >= 1
    assert "torch_threads" in report
    assert "omp_num_threads" in report
