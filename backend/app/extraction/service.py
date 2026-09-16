"""Phase 3 orchestration over normalized Phase 2 parsed documents."""

from sqlalchemy import select
from sqlalchemy.orm import Session
import logging

from app.core.pipeline import current_run, fingerprint
from app.core.logging import ProgressLogger

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
    pending = []
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
                pending.append(dict(text=sentence, section_type=section_type, section_heading=section_heading, section_index=section_index, paragraph_index=paragraph_index, sentence_index=sentence_index))
    if pending:
        matches = semantic_classifier.classify_many([row['text'] for row in pending])
        for row, match in zip(pending, matches, strict=True):
            if match is not None:
                extracted.append(ExtractedStatement(**row, fact_type=match.fact_type, confidence=match.confidence, extraction_method='semantic'))
    extracted.sort(key=lambda s: (s.section_index, s.paragraph_index, s.sentence_index, s.fact_type.value))
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
    classifier = semantic_classifier
    parsed_documents = list(session.scalars(
        select(ParsedDocument).order_by(ParsedDocument.paper_id)
    ))
    processed = []
    progress = ProgressLogger(logging.getLogger(__name__), 's3', len(parsed_documents))
    for position, parsed_document in enumerate(parsed_documents, 1):
        def extract():
            nonlocal classifier
            classifier = classifier or get_semantic_classifier()
            extract_and_store(session, parsed_document.paper_id, classifier)
        run = current_run()
        digest = fingerprint({'sections': parsed_document.sections, 'extraction': 'rules-scibert-v1-0.55-0.03'})
        if run is None:
            extract()
            processed.append(parsed_document.paper_id)
        elif run.paper(parsed_document.paper_id, 's3', digest, extract, output=lambda: [
            [f.id, f.fact_type, f.text, f.section_type, f.section_heading, f.section_index, f.paragraph_index, f.sentence_index, f.confidence, f.extraction_method]
            for f in session.scalars(select(ExtractedFact).where(ExtractedFact.paper_id == parsed_document.paper_id).order_by(ExtractedFact.id))
        ]):
            processed.append(parsed_document.paper_id)
        progress.update(position)
    return processed
