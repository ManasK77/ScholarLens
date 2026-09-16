"""Backfill metadata for manual uploads from GROBID header TEI."""

from pathlib import Path
from xml.etree import ElementTree as ET

from sqlalchemy.orm import Session

from app.ingestion import corpus, unpaywall
from app.ingestion.corpus import Paper
from app.parsing import grobid_client

NS = {"tei": "http://www.tei-c.org/ns/1.0"}


def _clean_text(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    value = " ".join("".join(element.itertext()).split())
    return value or None


def extract_header_fields(tei_xml: str) -> dict:
    """Extract title, author names, and optional DOI from GROBID TEI."""
    root = ET.fromstring(tei_xml)
    title = _clean_text(
        root.find(".//tei:teiHeader/tei:fileDesc/tei:titleStmt/tei:title", NS)
    )
    authors = []
    for person in root.findall(
        ".//tei:sourceDesc/tei:biblStruct/tei:analytic/tei:author/tei:persName", NS
    ):
        parts = [
            value
            for value in (
                _clean_text(forename) for forename in person.findall("tei:forename", NS)
            )
            if value
        ]
        surname = _clean_text(person.find("tei:surname", NS))
        if surname:
            parts.append(surname)
        if parts:
            authors.append(" ".join(parts))
    doi = _clean_text(root.find(".//tei:idno[@type='DOI']", NS))
    return {"title": title, "authors": authors, "doi": doi}


def backfill_metadata(
    session: Session,
    paper: Paper,
    grobid_url: str,
    unpaywall_email: str,
    pdf_storage_path: str,
) -> Paper | None:
    """Update an existing paper and reuse Phase 1's DOI enrichment pass."""
    if not paper.pdf_path:
        return None
    try:
        pdf_bytes = Path(paper.pdf_path).read_bytes()
    except OSError:
        return None
    tei_xml = grobid_client.parse_header(pdf_bytes, grobid_url)
    if tei_xml is None:
        return None
    try:
        fields = extract_header_fields(tei_xml)
    except ET.ParseError:
        return None
    if fields["title"] is None:
        return None

    updates = {"title": fields["title"], "authors": fields["authors"]}
    if fields["doi"]:
        updates["doi"] = fields["doi"]
    updated = corpus.enrich_paper(session, paper.id, updates)
    if fields["doi"]:
        unpaywall.enrich(session, unpaywall_email, pdf_storage_path, paper_id=paper.id)
    return updated
