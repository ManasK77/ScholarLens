from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.extraction import service
from app.extraction.schema import FactType, SemanticMatch
from app.extraction.storage import query_facts
from app.ingestion import corpus
from app.parsing.storage import upsert_parsed_document


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    corpus.Base.metadata.create_all(engine)
    with Session(engine) as active:
        yield active


def _paper(session: Session, title: str = "Paper"):
    return corpus.insert_paper(
        session,
        {
            "title": title,
            "authors": [],
            "year": 2024,
            "venue": None,
            "doi": None,
            "abstract": None,
            "source": "manual_upload",
            "pdf_path": "paper.pdf",
            "has_full_text": True,
            "arxiv_id": None,
            "acl_id": None,
        },
    )


def _sections() -> list[dict]:
    return [
        {
            "heading": "Experiments",
            "type": "Results",
            "paragraphs": [
                {
                    "text": "Our model achieves 92 F1 on SQuAD.",
                    "sentences": ["Our model achieves 92 F1 on SQuAD."],
                },
                {
                    "text": "Cross-domain behavior remains challenging.",
                    "sentences": ["Cross-domain behavior remains challenging."],
                },
            ],
        }
    ]


def test_extract_document_preserves_exact_evidence_and_coordinates() -> None:
    semantic = Mock()
    semantic.classify_many.return_value = [SemanticMatch(FactType.LIMITATION, 0.72)]
    statements = service.extract_sections(_sections(), semantic)

    first_sentence = [
        statement
        for statement in statements
        if statement.text == "Our model achieves 92 F1 on SQuAD."
    ]
    assert {statement.fact_type for statement in first_sentence} == {
        FactType.CONTRIBUTION,
        FactType.MODEL,
        FactType.METRIC,
        FactType.DATASET,
    }
    assert all(statement.paragraph_index == 0 for statement in first_sentence)
    assert all(statement.sentence_index == 0 for statement in first_sentence)
    assert all(statement.section_index == 0 for statement in first_sentence)
    assert all(statement.section_type == "Results" for statement in first_sentence)

    semantic_statement = next(
        statement
        for statement in statements
        if statement.extraction_method == "semantic"
    )
    assert semantic_statement.text == "Cross-domain behavior remains challenging."
    assert semantic_statement.paragraph_index == 1
    assert semantic_statement.confidence == pytest.approx(0.72)
    semantic.classify_many.assert_called_once_with(
        ["Cross-domain behavior remains challenging."]
    )


def test_fallback_paragraph_text_is_sentence_segmented() -> None:
    sections = [
        {
            "heading": None,
            "type": "Future Work",
            "paragraphs": [
                {
                    "text": "One neutral sentence. We should explore multilingual data.",
                    "sentences": [],
                }
            ],
        }
    ]
    statements = service.extract_sections(sections, Mock())
    future = [s for s in statements if s.fact_type is FactType.FUTURE_WORK]
    assert [statement.text for statement in future] == [
        "One neutral sentence.",
        "We should explore multilingual data.",
    ]
    assert [statement.sentence_index for statement in future] == [0, 1]


def test_extract_and_store_reads_phase2_document(session: Session) -> None:
    paper = _paper(session)
    upsert_parsed_document(session, paper.id, _sections(), "grobid")
    semantic = Mock()
    semantic.classify_many.return_value = [None]

    stored = service.extract_and_store(session, paper.id, semantic)

    assert stored
    assert query_facts(session, paper.id, FactType.METRIC)


def test_extract_and_store_rejects_unparsed_paper(session: Session) -> None:
    paper = _paper(session)
    with pytest.raises(ValueError, match="no parsed document"):
        service.extract_and_store(session, paper.id, Mock())


def test_extract_all_parsed_processes_every_parsed_paper(
    session: Session, monkeypatch
) -> None:
    first = _paper(session, "First")
    second = _paper(session, "Second")
    upsert_parsed_document(session, first.id, _sections(), "grobid")
    upsert_parsed_document(session, second.id, _sections(), "fallback")
    extract = Mock(return_value=[])
    monkeypatch.setattr(service, "extract_and_store", extract)
    semantic = Mock()

    processed = service.extract_all_parsed(session, semantic)

    assert processed == [first.id, second.id]
    # The trailing argument is the O2 telemetry sink the stage accumulates
    # sentence/rule/semantic counts into; the same dict is shared by every call.
    assert [call.args[:3] for call in extract.call_args_list] == [
        (session, first.id, semantic),
        (session, second.id, semantic),
    ]
    assert all(isinstance(call.args[3], dict) for call in extract.call_args_list)
