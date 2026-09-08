import pytest
from numpy import float32
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.extraction.schema import ExtractedStatement, FactType
from app.extraction.storage import ExtractedFact, query_facts, replace_facts
from app.ingestion import corpus


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    corpus.Base.metadata.create_all(engine)
    with Session(engine) as active:
        yield active


def _paper(session: Session):
    return corpus.insert_paper(
        session,
        {
            "title": "Paper",
            "authors": [],
            "year": 2024,
            "venue": "ACL",
            "doi": None,
            "abstract": None,
            "source": "arxiv",
            "pdf_path": "paper.pdf",
            "has_full_text": True,
            "arxiv_id": None,
            "acl_id": None,
        },
    )


def _statement(fact_type: FactType, text: str) -> ExtractedStatement:
    return ExtractedStatement(
        fact_type=fact_type,
        text=text,
        section_type="Results",
        section_heading="Experiments",
        section_index=0,
        paragraph_index=0,
        sentence_index=1,
        confidence=0.95,
        extraction_method="rule",
    )


def test_replace_and_query_facts_by_type(session: Session) -> None:
    paper = _paper(session)
    replace_facts(
        session,
        paper.id,
        [
            _statement(FactType.CONTRIBUTION, "We propose a model."),
            _statement(FactType.METRIC, "It reaches 92 F1."),
        ],
    )

    metrics = query_facts(session, paper.id, FactType.METRIC)

    assert len(metrics) == 1
    assert metrics[0].text == "It reaches 92 F1."
    assert metrics[0].paper_id == paper.id
    assert metrics[0].fact_type == FactType.METRIC.value


def test_replacement_is_idempotent_and_removes_stale_rows(session: Session) -> None:
    paper = _paper(session)
    replace_facts(
        session, paper.id, [_statement(FactType.LIMITATION, "Old limitation.")]
    )
    replace_facts(
        session, paper.id, [_statement(FactType.FUTURE_WORK, "New future work.")]
    )

    facts = query_facts(session, paper.id)

    assert [(fact.fact_type, fact.text) for fact in facts] == [
        (FactType.FUTURE_WORK.value, "New future work.")
    ]


def test_duplicate_statements_are_suppressed(session: Session) -> None:
    paper = _paper(session)
    statement = _statement(FactType.MODEL, "We use BERT.")
    replace_facts(session, paper.id, [statement, statement])
    assert session.query(ExtractedFact).filter_by(paper_id=paper.id).count() == 1


def test_numpy_confidence_is_normalized_before_database_persistence(
    session: Session,
) -> None:
    """SciBERT cosine scores are NumPy scalars; psycopg2 needs native floats."""
    paper = _paper(session)
    statement = _statement(FactType.LIMITATION, "A semantic limitation.")
    statement = ExtractedStatement(
        fact_type=statement.fact_type,
        text=statement.text,
        section_type=statement.section_type,
        section_heading=statement.section_heading,
        section_index=statement.section_index,
        paragraph_index=statement.paragraph_index,
        sentence_index=statement.sentence_index,
        confidence=float32(0.72),
        extraction_method="semantic",
    )

    rows = replace_facts(session, paper.id, [statement])

    assert type(rows[0].confidence) is float
    assert rows[0].confidence == pytest.approx(0.72)


def test_same_evidence_location_in_different_sections_is_preserved(
    session: Session,
) -> None:
    paper = _paper(session)
    first = _statement(FactType.MODEL, "We use BERT.")
    second = ExtractedStatement(
        fact_type=first.fact_type,
        text=first.text,
        section_type=first.section_type,
        section_heading="Ablations",
        section_index=1,
        paragraph_index=first.paragraph_index,
        sentence_index=first.sentence_index,
        confidence=first.confidence,
        extraction_method=first.extraction_method,
    )

    replace_facts(session, paper.id, [first, second])

    facts = query_facts(session, paper.id, FactType.MODEL)
    assert [(fact.section_index, fact.section_heading) for fact in facts] == [
        (0, "Experiments"),
        (1, "Ablations"),
    ]


def test_fact_table_foreign_keys_to_papers() -> None:
    foreign_keys = ExtractedFact.__table__.c.paper_id.foreign_keys
    assert any(key.column.table.name == "papers" for key in foreign_keys)
