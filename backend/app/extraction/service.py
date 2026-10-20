"""Phase 3 orchestration over normalized Phase 2 parsed documents."""

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import ProgressLogger
from app.core.pipeline import current_run, fingerprint
from app.extraction.rules import extract_rule_types, split_sentences
from app.extraction.schema import ExtractedStatement
from app.extraction.semantic import (
    SemanticStatementClassifier,
    get_semantic_classifier,
)
from app.extraction.storage import ExtractedFact, replace_facts
from app.parsing.section_classifier import _keyword_match
from app.parsing.storage import ParsedDocument, get_parsed_document

# Bump when what a stored fact means changes, so the S3 checkpoint re-runs.
EXTRACTION_VERSION = "rules-scibert-v1-0.55-0.03-prior-keyword-v1"


def _prior_section_type(section_type: str | None, heading: str | None) -> str | None:
    """The section type S3 may treat as a per-sentence prior.

    Stage 2 maps a heading its keywords miss onto the nearest of seven labels,
    so "Ethics Statement" or a table header can arrive typed `Limitations`.
    That guess must not label every sentence beneath it a limitation.
    """
    if heading and heading.strip() and _keyword_match(heading) != section_type:
        return None
    return section_type


def extract_sections(
    sections: list[dict],
    semantic_classifier: SemanticStatementClassifier | None,
    stats: dict[str, int] | None = None,
) -> list[ExtractedStatement]:
    """Extract exact, typed evidence statements from Phase 2's JSON structure.

    `stats` is an optional O2 telemetry sink: the caller passes a dict and this
    adds counts (never text) to it, so a stage can report sentences seen, rule
    hits and semantic calls without a second pass over the document.
    """
    extracted: list[ExtractedStatement] = []
    pending = []
    counts = stats if stats is not None else {}
    for section_index, section in enumerate(sections):
        section_type = section.get("type")
        section_heading = section.get("heading")
        prior_type = _prior_section_type(section_type, section_heading)
        for paragraph_index, paragraph in enumerate(section.get("paragraphs", [])):
            sentences = paragraph.get("sentences") or split_sentences(
                paragraph.get("text", "")
            )
            for sentence_index, raw_sentence in enumerate(sentences):
                sentence = " ".join(raw_sentence.split())
                if not sentence:
                    continue
                counts["sentences"] = counts.get("sentences", 0) + 1
                fact_types = extract_rule_types(sentence, prior_type)
                if fact_types:
                    counts["rule_hits"] = counts.get("rule_hits", 0) + 1
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
                pending.append(
                    {
                        "text": sentence,
                        "section_type": section_type,
                        "section_heading": section_heading,
                        "section_index": section_index,
                        "paragraph_index": paragraph_index,
                        "sentence_index": sentence_index,
                    }
                )
    counts["semantic_calls"] = counts.get("semantic_calls", 0) + len(pending)
    if pending:
        matches = semantic_classifier.classify_many([row["text"] for row in pending])
        for row, match in zip(pending, matches, strict=True):
            if match is not None:
                extracted.append(
                    ExtractedStatement(
                        **row,
                        fact_type=match.fact_type,
                        confidence=match.confidence,
                        extraction_method="semantic",
                    )
                )
    extracted.sort(
        key=lambda s: (
            s.section_index,
            s.paragraph_index,
            s.sentence_index,
            s.fact_type.value,
        )
    )
    return extracted


def extract_and_store(
    session: Session,
    paper_id: int,
    semantic_classifier: SemanticStatementClassifier | None = None,
    stats: dict[str, int] | None = None,
) -> list[ExtractedFact]:
    parsed_document = get_parsed_document(session, paper_id)
    if parsed_document is None:
        raise ValueError(f"Paper {paper_id} has no parsed document")
    classifier = semantic_classifier or get_semantic_classifier()
    statements = extract_sections(parsed_document.sections, classifier, stats)
    return replace_facts(session, paper_id, statements)


def extract_all_parsed(
    session: Session,
    semantic_classifier: SemanticStatementClassifier | None = None,
) -> list[int]:
    """Re-extract every parsed paper using one shared semantic classifier."""
    classifier = semantic_classifier
    parsed_documents = list(
        session.scalars(select(ParsedDocument).order_by(ParsedDocument.paper_id))
    )
    processed = []
    logger = logging.getLogger(__name__)
    logger.info("s3 start parsed_documents=%d", len(parsed_documents))
    stats: dict[str, int] = {}
    progress = ProgressLogger(logger, "s3", len(parsed_documents))
    for position, parsed_document in enumerate(parsed_documents, 1):
        # `paper_id` is bound as a default argument rather than closed over:
        # both callables are handed to `run.paper` and must describe *this*
        # iteration's document even though the loop variable moves on.
        def extract(paper_id=parsed_document.paper_id):
            nonlocal classifier
            classifier = classifier or get_semantic_classifier()
            extract_and_store(session, paper_id, classifier, stats)

        def stored_facts(paper_id=parsed_document.paper_id):
            return [
                [
                    f.id,
                    f.fact_type,
                    f.text,
                    f.section_type,
                    f.section_heading,
                    f.section_index,
                    f.paragraph_index,
                    f.sentence_index,
                    f.confidence,
                    f.extraction_method,
                ]
                for f in session.scalars(
                    select(ExtractedFact)
                    .where(ExtractedFact.paper_id == paper_id)
                    .order_by(ExtractedFact.id)
                )
            ]

        run = current_run()
        digest = fingerprint(
            {
                "sections": parsed_document.sections,
                "extraction": EXTRACTION_VERSION,
            }
        )
        if run is None:
            extract()
            processed.append(parsed_document.paper_id)
        elif run.paper(
            parsed_document.paper_id, "s3", digest, extract, output=stored_facts
        ):
            processed.append(parsed_document.paper_id)
        progress.update(position)
    by_type = session.execute(
        select(ExtractedFact.fact_type, func.count()).group_by(ExtractedFact.fact_type)
    ).all()
    logger.info(
        "s3 complete papers=%d sentences=%d rule_hits=%d semantic_calls=%d corpus_facts=[%s]",
        len(processed),
        stats.get("sentences", 0),
        stats.get("rule_hits", 0),
        stats.get("semantic_calls", 0),
        " ".join(f"{fact_type}={count}" for fact_type, count in sorted(by_type)),
    )
    return processed
