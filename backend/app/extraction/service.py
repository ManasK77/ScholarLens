"""Phase 3 orchestration over normalized Phase 2 parsed documents."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extraction.rules import extract_rule_types, split_sentences
from app.extraction.schema import ExtractedStatement
from app.extraction.semantic import (
    SemanticStatementClassifier,
    get_semantic_classifier,
)
from app.extraction.storage import ExtractedFact, replace_facts
from app.parsing.storage import ParsedDocument, get_parsed_document


def extract_sections(
    sections: list[dict], semantic_classifier: SemanticStatementClassifier | None
) -> list[ExtractedStatement]:
    """Extract exact, typed evidence statements from Phase 2's JSON structure."""
    extracted: list[ExtractedStatement] = []
    for section_index, section in enumerate(sections):
        section_type = section.get("type")
        section_heading = section.get("heading")
        for paragraph_index, paragraph in enumerate(section.get("paragraphs", [])):
            sentences = paragraph.get("sentences") or split_sentences(
                paragraph.get("text", "")
            )
            for sentence_index, raw_sentence in enumerate(sentences):
                sentence = " ".join(raw_sentence.split())
                if not sentence:
                    continue
                fact_types = extract_rule_types(sentence, section_type)
                if fact_types:
                    for fact_type in sorted(fact_types, key=lambda value: value.value):
                        extracted.append(
                            ExtractedStatement(
                                fact_type=fact_type,
                                text=sentence,
                                section_type=section_type,
                                section_heading=section_heading,
                                section_index=section_index,
                                paragraph_index=paragraph_index,
                                sentence_index=sentence_index,
                                confidence=1.0,
                                extraction_method="rule",
                            )
                        )
                    continue
                if semantic_classifier is None:
                    continue
                semantic_match = semantic_classifier.classify(sentence)
                if semantic_match is not None:
                    extracted.append(
                        ExtractedStatement(
                            fact_type=semantic_match.fact_type,
                            text=sentence,
                            section_type=section_type,
                            section_heading=section_heading,
                            section_index=section_index,
                            paragraph_index=paragraph_index,
                            sentence_index=sentence_index,
                            confidence=semantic_match.confidence,
                            extraction_method="semantic",
                        )
                    )
    return extracted


def extract_and_store(
    session: Session,
    paper_id: int,
    semantic_classifier: SemanticStatementClassifier | None = None,
) -> list[ExtractedFact]:
    parsed_document = get_parsed_document(session, paper_id)
    if parsed_document is None:
        raise ValueError(f"Paper {paper_id} has no parsed document")
    classifier = semantic_classifier or get_semantic_classifier()
    statements = extract_sections(parsed_document.sections, classifier)
    return replace_facts(session, paper_id, statements)


def extract_all_parsed(
    session: Session,
    semantic_classifier: SemanticStatementClassifier | None = None,
) -> list[int]:
    """Re-extract every parsed paper using one shared semantic classifier."""
    classifier = semantic_classifier or get_semantic_classifier()
    parsed_documents = session.scalars(
        select(ParsedDocument).order_by(ParsedDocument.paper_id)
    )
    processed = []
    for parsed_document in parsed_documents:
        extract_and_store(session, parsed_document.paper_id, classifier)
        processed.append(parsed_document.paper_id)
    return processed
