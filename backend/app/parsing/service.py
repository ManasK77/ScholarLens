"""Source-agnostic Stage 2 parsing orchestration."""

import logging
import hashlib
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingestion.corpus import Paper
from app.core.pipeline import current_run, fingerprint
from app.core.logging import ProgressLogger
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
    labels = section_classifier.classify_sections([section.get('heading') for section in raw_sections])
    for raw_section, label in zip(raw_sections, labels, strict=True):
        section = dict(raw_section)
        section["type"] = label
        normalized_sections.append(section)
    return storage.upsert_parsed_document(
        session,
        paper.id,
        sections=normalized_sections,
        parser_used=parser_used,
    )


def _needs_metadata_backfill(paper: Paper) -> bool:
    """D-32/PARSE-05 intentionally scopes late metadata recovery to manual
    uploads. This is Stage 2's only source check; the parsing core stays fully
    source-agnostic."""
    return paper.source == "manual_upload" and paper.title is None


def run_parsing_pipeline(
    session: Session,
    paper: Paper,
    grobid_url: str,
    unpaywall_email: str,
    pdf_storage_path: str,
) -> ParsedDocument:
    document = parse_and_store(session, paper, grobid_url)
    if _needs_metadata_backfill(paper):
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
    papers = list(session.scalars(select(Paper).order_by(Paper.id)))
    progress = ProgressLogger(logger, 's2', len(papers))
    for position, paper in enumerate(papers, 1):
        progress.update(position)
        run = current_run()
        if run and paper.pdf_path and Path(paper.pdf_path).is_file():
            with Path(paper.pdf_path).open('rb') as pdf:
                pdf_hash = hashlib.file_digest(pdf, 'sha256').hexdigest()
            input_hash = fingerprint({'pdf': pdf_hash, 'parser': 'grobid-0.9.0/fallback-v1', 'headings': 'scibert-v1'})
            if run.paper(paper.id, 's2', input_hash, lambda: run_parsing_pipeline(session, paper, grobid_url, unpaywall_email, pdf_storage_path), verify=lambda: storage.get_parsed_document(session, paper.id) is not None, output=lambda: storage.get_parsed_document(session, paper.id).sections):
                processed.append(paper.id)
            elif _needs_metadata_backfill(paper):
                # Header-only retries preserve the already parsed evidence.
                header_backfill.backfill_metadata(session, paper, grobid_url, unpaywall_email, pdf_storage_path)
                session.commit()
            continue
        if storage.get_parsed_document(session, paper.id) is not None:
            # A document stored while GROBID was unreachable came from the
            # fallback parser, and its header backfill silently did nothing.
            # Retry just the header call, or metadata GROBID can recover is
            # never recovered on any later run.
            if (
                _needs_metadata_backfill(paper)
                and paper.pdf_path
                and Path(paper.pdf_path).is_file()
            ):
                header_backfill.backfill_metadata(
                    session, paper, grobid_url, unpaywall_email, pdf_storage_path
                )
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
