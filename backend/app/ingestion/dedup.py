"""Deduplication (D-19, INGEST-02): DOI -> arXiv ID -> ACL ID -> normalized
title+year priority chain, plus the field-merge rule for a matched row.
"""

import re
import string

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingestion.corpus import Paper

_PUNCTUATION_TABLE = str.maketrans("", "", string.punctuation)


def normalize_title(title: str) -> str:
    collapsed = re.sub(r"\s+", " ", title.strip().lower())
    return collapsed.translate(_PUNCTUATION_TABLE)


def find_existing(session: Session, candidate: dict) -> Paper | None:
    doi = candidate.get("doi")
    if doi:
        existing = session.scalars(select(Paper).where(Paper.doi == doi)).first()
        if existing:
            return existing

    arxiv_id = candidate.get("arxiv_id")
    if arxiv_id:
        existing = session.scalars(
            select(Paper).where(Paper.arxiv_id == arxiv_id)
        ).first()
        if existing:
            return existing

    acl_id = candidate.get("acl_id")
    if acl_id:
        existing = session.scalars(select(Paper).where(Paper.acl_id == acl_id)).first()
        if existing:
            return existing

    title = candidate.get("title")
    year = candidate.get("year")
    if title and year is not None:
        normalized_candidate = normalize_title(title)
        for paper in session.scalars(select(Paper).where(Paper.year == year)):
            if paper.title and normalize_title(paper.title) == normalized_candidate:
                return paper

    return None


def merge(existing, candidate: dict) -> dict:
    result: dict = {}
    for key, candidate_value in candidate.items():
        existing_value = getattr(existing, key, None)
        if candidate_value is not None and existing_value is None:
            result[key] = candidate_value
        else:
            result[key] = existing_value

    if getattr(existing, "has_full_text", False):
        result["has_full_text"] = True
        result["pdf_path"] = existing.pdf_path
    elif candidate.get("has_full_text"):
        result["has_full_text"] = True
        result["pdf_path"] = candidate.get("pdf_path")

    return result
