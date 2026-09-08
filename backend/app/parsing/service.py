"""Source-agnostic Stage 2 parsing orchestration."""

import logging
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingestion.corpus import Paper
from app.parsing import (
    fallback,
    grobid_client,
    header_backfill,
    section_classifier,
    storage,
    tei_parser,
)
from app.parsing.storage import ParsedDocument

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ParsingBatch:
    processed: list[int]
    missing_pdfs: list[int]


def parse_and_store(session: Session, paper: Paper, grobid_url: str) -> ParsedDocument:
    """Parse and normalize one paper identically regardless of provenance."""
    if not paper.pdf_path:
        raise ValueError(f"Paper {paper.id} has no stored PDF")
    pdf_bytes = Path(paper.pdf_path).read_bytes()
    tei_xml = grobid_client.parse_fulltext(pdf_bytes, grobid_url)
    raw_sections = None
    if tei_xml is not None:
        try:
            raw_sections = tei_parser.extract_sections(tei_xml)
        except ET.ParseError:
            raw_sections = None

    if not raw_sections:
        raw_sections = fallback.extract_fallback_sections(pdf_bytes) or []
        parser_used = "fallback"
    else:
        parser_used = "grobid"

    normalized_sections = []
    for raw_section in raw_sections:
        section = dict(raw_section)
        section["type"] = section_classifier.classify_section(section.get("heading"))
        normalized_sections.append(section)
    return storage.upsert_parsed_document(
        session,
        paper.id,
        sections=normalized_sections,
        parser_used=parser_used,
    )


def run_parsing_pipeline(
    session: Session,
    paper: Paper,
    grobid_url: str,
    unpaywall_email: str,
    pdf_storage_path: str,
) -> ParsedDocument:
    document = parse_and_store(session, paper, grobid_url)
    # D-32/PARSE-05 intentionally scopes late metadata recovery to manual
    # uploads. The parsing core above remains fully source-agnostic.
    if paper.source == "manual_upload" and paper.title is None:
        header_backfill.backfill_metadata(
            session, paper, grobid_url, unpaywall_email, pdf_storage_path
        )
    return document


def parse_all_pending(
    session: Session,
    grobid_url: str,
    unpaywall_email: str,
    pdf_storage_path: str,
) -> ParsingBatch:
    """Parse pending full-text papers and reconcile stale file references."""
    processed: list[int] = []
    missing_pdfs: list[int] = []
    for paper in session.scalars(select(Paper)):
        if storage.get_parsed_document(session, paper.id) is not None:
            continue
        if not paper.has_full_text:
            continue
        if not paper.pdf_path or not Path(paper.pdf_path).is_file():
            logger.warning(
                "Paper %s references a missing PDF at %s; marking it metadata-only",
                paper.id,
                paper.pdf_path,
            )
            paper.has_full_text = False
            paper.pdf_path = None
            missing_pdfs.append(paper.id)
            continue
        try:
            run_parsing_pipeline(
                session, paper, grobid_url, unpaywall_email, pdf_storage_path
            )
        except FileNotFoundError:
            # The file may disappear between the existence check and the read.
            logger.warning(
                "Paper %s PDF disappeared during parsing; marking it metadata-only",
                paper.id,
            )
            paper.has_full_text = False
            paper.pdf_path = None
            missing_pdfs.append(paper.id)
            continue
        processed.append(paper.id)
    return ParsingBatch(processed, missing_pdfs)
