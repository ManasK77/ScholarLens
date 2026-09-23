"""Source-agnostic Stage 2 parsing orchestration."""

import hashlib
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import ProgressLogger
from app.core.pipeline import current_run, fingerprint
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
    labels = section_classifier.classify_sections(
        [section.get("heading") for section in raw_sections]
    )
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
    # One aggregate rather than a lookup per paper: this line is telemetry and
    # must not add a query to every paper in the corpus.
    already_parsed = (
        session.scalar(select(func.count()).select_from(ParsedDocument)) or 0
    )
    logger.info(
        "s2 start papers=%d parsed=%d pending=%d",
        len(papers),
        already_parsed,
        len(papers) - already_parsed,
    )
    # Counts only (§4.1): never a heading, a title or a path.
    headings = section_classifier.get_heading_classifier()
    heading_baseline = (headings.keyword_count, headings.semantic_count)
    parsers: Counter[str] = Counter()
    backfill_attempted = backfill_succeeded = 0
    progress = ProgressLogger(logger, "s2", len(papers))
    for position, paper in enumerate(papers, 1):
        progress.update(position)
        needs_backfill = _needs_metadata_backfill(paper)
        parsed_before = len(processed)
        try:
            _parse_one(
                session,
                paper,
                grobid_url,
                unpaywall_email,
                pdf_storage_path,
                processed,
                missing_pdfs,
            )
        finally:
            if needs_backfill:
                backfill_attempted += 1
                backfill_succeeded += paper.title is not None
            if len(processed) > parsed_before:
                document = storage.get_parsed_document(session, paper.id)
                if document is not None:
                    parsers[document.parser_used] += 1
    logger.info(
        "s2 complete processed=%d grobid=%d fallback=%d missing_pdfs=%d "
        "backfill_attempted=%d backfill_succeeded=%d heading_keyword=%d "
        "heading_semantic=%d",
        len(processed),
        parsers["grobid"],
        parsers["fallback"],
        len(missing_pdfs),
        backfill_attempted,
        backfill_succeeded,
        headings.keyword_count - heading_baseline[0],
        headings.semantic_count - heading_baseline[1],
    )
    return ParsingBatch(processed, missing_pdfs)


def _parse_one(
    session: Session,
    paper: Paper,
    grobid_url: str,
    unpaywall_email: str,
    pdf_storage_path: str,
    processed: list[int],
    missing_pdfs: list[int],
) -> None:
    """One paper's Stage 2 decision tree, appending to the batch's outcomes."""
    run = current_run()
    if run and paper.pdf_path and Path(paper.pdf_path).is_file():
        with Path(paper.pdf_path).open("rb") as pdf:
            pdf_hash = hashlib.file_digest(pdf, "sha256").hexdigest()
        input_hash = fingerprint(
            {
                "pdf": pdf_hash,
                "parser": "grobid-0.9.0/fallback-v1",
                "headings": "scibert-v1",
            }
        )
        if run.paper(
            paper.id,
            "s2",
            input_hash,
            lambda: run_parsing_pipeline(
                session, paper, grobid_url, unpaywall_email, pdf_storage_path
            ),
            verify=lambda: storage.get_parsed_document(session, paper.id) is not None,
            output=lambda: storage.get_parsed_document(session, paper.id).sections,
        ):
            processed.append(paper.id)
        elif _needs_metadata_backfill(paper):
            # Header-only retries preserve the already parsed evidence.
            header_backfill.backfill_metadata(
                session, paper, grobid_url, unpaywall_email, pdf_storage_path
            )
            session.commit()
        return
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
        return
    if not paper.has_full_text:
        return
    if not paper.pdf_path or not Path(paper.pdf_path).is_file():
        logger.warning(
            "Paper %s references a missing PDF at %s; marking it metadata-only",
            paper.id,
            paper.pdf_path,
        )
        paper.has_full_text = False
        paper.pdf_path = None
        missing_pdfs.append(paper.id)
        return
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
        return
    processed.append(paper.id)
