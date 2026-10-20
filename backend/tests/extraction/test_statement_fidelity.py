"""What Stage 3 keeps of a sentence, and what it records about where it was.

Characterization tests (S2-S5 audit, 2026-10-13): Stage 3 had no defect here,
but nothing pinned these properties, and Stages 4-9 rely on every one of them.
A statement is the source sentence itself -- whitespace-normalized, never
paraphrased -- so negation, hedging and conditions survive by construction.
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.extraction.schema import FactType
from app.extraction.semantic import SemanticStatementClassifier
from app.extraction.service import extract_and_store, extract_sections
from app.extraction.storage import ExtractedFact
from app.ingestion.corpus import Base, Paper
from app.parsing.storage import upsert_parsed_document


def _section(*sentences, heading="Discussion", kind="Discussion"):
    return {
        "heading": heading,
        "type": kind,
        "paragraphs": [{"text": " ".join(sentences), "sentences": list(sentences)}],
    }


def _texts(statements, fact_type):
    return [s.text for s in statements if s.fact_type == fact_type]


@pytest.mark.parametrize(
    "sentence",
    [
        # negation
        "Our detector cannot identify attacks written in Tamil.",
        # uncertainty / hedging
        "The method may fail to transfer to low-resource languages.",
        # a condition
        "Performance degrades only when the prompt mixes two scripts.",
    ],
)
def test_limitations_keep_their_negation_hedging_and_conditions_verbatim(sentence):
    statements = extract_sections([_section(sentence)], None)
    assert _texts(statements, FactType.LIMITATION) == [sentence]


def test_contradictory_statements_are_both_kept_with_their_own_locations():
    """Stage 3 records both sides; resolving them is Stage 6's NLI, not this."""
    sections = [
        _section("The defense fails on every code-switched prompt."),
        _section("The defense fails on no code-switched prompt we tested."),
    ]
    statements = extract_sections(sections, None)
    limitations = [s for s in statements if s.fact_type == FactType.LIMITATION]
    assert [s.text for s in limitations] == [
        "The defense fails on every code-switched prompt.",
        "The defense fails on no code-switched prompt we tested.",
    ]
    assert [s.section_index for s in limitations] == [0, 1]


def test_a_negated_failure_is_not_typed_a_limitation_by_the_cue_rules():
    """'does not fail on' matches no limitation cue: it is not a limitation."""
    statements = extract_sections(
        [_section("Our defense does not fail on code-switched prompts.")], None
    )
    assert _texts(statements, FactType.LIMITATION) == []


def test_cited_claims_are_kept_verbatim_and_are_not_typed_as_cited():
    """Known limitation: the schema has no 'cited claim' type.

    A sentence reporting *another* paper's limitation is stored like the
    paper's own, with its citation marker intact so a reader can tell. Stage 6
    clustering does not separate the two either (README scope; documented).
    """
    sentence = "Prior detectors [12] fail to generalize across languages."
    statements = extract_sections([_section(sentence)], None)
    assert _texts(statements, FactType.LIMITATION) == [sentence]


def test_future_work_is_typed_and_attributed_to_its_location():
    sections = [
        _section("We introduce a benchmark."),
        _section(
            "Future work should explore Hindi-English code-mixing.",
            heading="Conclusion",
            kind="Conclusion",
        ),
    ]
    statements = extract_sections(sections, None)
    future = [s for s in statements if s.fact_type == FactType.FUTURE_WORK]
    assert [
        (
            s.text,
            s.section_index,
            s.paragraph_index,
            s.sentence_index,
            s.section_heading,
        )
        for s in future
    ] == [
        ("Future work should explore Hindi-English code-mixing.", 1, 0, 0, "Conclusion")
    ]


def test_empty_and_whitespace_input_yields_no_statement():
    sections = [
        {"heading": None, "type": None, "paragraphs": []},
        {
            "heading": "Limitations",
            "type": "Limitations",
            "paragraphs": [{"text": "   ", "sentences": ["  ", ""]}],
        },
    ]
    assert extract_sections(sections, None) == []
    assert extract_sections([], None) == []


def test_repeated_text_at_two_locations_is_two_statements_but_one_location_is_one(
    tmp_path,
):
    """Deduplication never collapses distinct locations -- nor stores one twice."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sentence = "The approach fails on unseen scripts."
    with Session(engine) as session:
        session.add(Paper(id=1, source="arxiv"))
        session.flush()
        upsert_parsed_document(
            session,
            1,
            [_section(sentence, sentence), _section(sentence)],
            "grobid",
        )
        extract_and_store(
            session,
            1,
            SemanticStatementClassifier(lambda texts: [[1.0] for _ in texts]),
        )
        rows = session.scalars(
            select(ExtractedFact).where(ExtractedFact.fact_type == "limitation")
        ).all()
    assert sorted((r.section_index, r.sentence_index) for r in rows) == [
        (0, 0),
        (0, 1),
        (1, 0),
    ]


def test_a_classifier_returning_the_wrong_number_of_vectors_fails_loudly():
    """Malformed model output must not silently mis-assign labels to sentences."""
    broken = SemanticStatementClassifier(lambda texts: [[1.0, 0.0]] * 7)
    with pytest.raises(ValueError, match="count mismatch"):
        extract_sections(
            [_section("A plain sentence about nothing in particular.")], broken
        )
