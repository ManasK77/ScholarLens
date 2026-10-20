"""Deduplication (D-19, INGEST-02): DOI -> arXiv ID -> ACL ID -> normalized
title+year priority chain, plus the field-merge rule for a matched row.

Identifiers are compared normalized (S1 audit, 2026-10-10). The same paper
reaches the corpus under several spellings: arXiv rows keep the versioned ID
the source returned ("2510.22628v2"); Semantic Scholar names an arXiv paper by
its DataCite DOI ("10.48550/arXiv.2510.22628") and an Anthology paper by its
ACL DOI ("10.18653/v1/2024.wmt-1.30"); DOIs differ in case and resolver prefix;
and a preprint's year is often a year before its venue's (a title match across
years also needs a shared author). Stored values are
never rewritten -- the version a source returned is provenance -- only the
comparison is normalized.
"""

import re

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.ingestion.corpus import Paper

_NON_WORD = re.compile(r"[\W_]+")
_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", re.IGNORECASE)
_ARXIV_PREFIX = re.compile(r"^arxiv:", re.IGNORECASE)
_ARXIV_VERSION = re.compile(r"v\d+$")
ARXIV_DOI_PREFIX = "10.48550/arxiv."
ACL_DOI_PREFIX = "10.18653/v1/"
# A title this long is specific enough to match a copy dated a year apart; a
# shorter one ("Deep Learning for NLP") is matched on the same year only.
MIN_TITLE_WORDS_FOR_YEAR_TOLERANCE = 6
# Resolver spellings stripped from a stored DOI before it is compared (lowercase,
# as the comparison is).
_STORED_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
)


def _title_words(title: str) -> list[str]:
    """Lowercase words; any punctuation (Unicode too) separates them."""
    return _NON_WORD.sub(" ", title.lower()).split()


def normalize_title(title: str) -> str:
    """A title's comparison key: its words, lowercase, with no separators.

    Punctuation variants must agree both ways: "Pre-training"/"Pretraining"
    and "Don't"/"Dont" (so a hyphen or apostrophe is not a word break), and
    "Qwen -- TF-IDF"/"Qwen--TF-IDF" (so spacing around punctuation is ignored).
    Dropping every separator satisfies both.
    """
    return "".join(_title_words(title))


def _author_keys(authors) -> set[str]:
    """Surnames, lowercased: "Dana Lee", "D. Lee" and "Lee, Dana" all give "lee"."""
    keys = set()
    for author in authors or ():
        name = str(author).split(",")[0] if "," in str(author) else str(author)
        words = _title_words(name)
        if words:
            keys.add(words[-1])
    return keys


def normalize_doi(doi: str | None) -> str | None:
    doi = _DOI_PREFIX.sub("", (doi or "").strip()).lower()
    return doi or None


def normalize_arxiv_id(arxiv_id: str | None) -> str | None:
    """`arXiv:2510.22628v2` -> `2510.22628`."""
    value = _ARXIV_PREFIX.sub("", (arxiv_id or "").strip()).lower()
    return _ARXIV_VERSION.sub("", value) or None


def _arxiv_ids(candidate: dict, doi: str | None) -> list[str]:
    ids = [normalize_arxiv_id(candidate.get("arxiv_id"))]
    if doi and doi.startswith(ARXIV_DOI_PREFIX):
        ids.append(normalize_arxiv_id(doi.removeprefix(ARXIV_DOI_PREFIX)))
    return list(dict.fromkeys(i for i in ids if i))


def _acl_ids(candidate: dict, doi: str | None) -> list[str]:
    ids = [(candidate.get("acl_id") or "").strip().lower()]
    if doi and doi.startswith(ACL_DOI_PREFIX):
        ids.append(doi.removeprefix(ACL_DOI_PREFIX))
    return list(dict.fromkeys(i for i in ids if i))


def _stored_doi():
    """`Paper.doi` as `normalize_doi` would read it."""
    expression = func.lower(Paper.doi)
    for prefix in _STORED_DOI_PREFIXES:
        expression = func.replace(expression, prefix, "")
    return expression


def find_existing(session: Session, candidate: dict) -> Paper | None:
    stored_doi = _stored_doi()
    doi = normalize_doi(candidate.get("doi"))
    if doi:
        existing = session.scalars(select(Paper).where(stored_doi == doi)).first()
        if existing:
            return existing

    for arxiv_id in _arxiv_ids(candidate, doi):
        stored_id = func.lower(Paper.arxiv_id)
        existing = session.scalars(
            select(Paper).where(
                or_(
                    stored_id == arxiv_id,
                    stored_id.like(f"{arxiv_id}v%"),
                    stored_doi == f"{ARXIV_DOI_PREFIX}{arxiv_id}",
                )
            )
        ).first()
        if existing:
            return existing

    for acl_id in _acl_ids(candidate, doi):
        existing = session.scalars(
            select(Paper).where(
                or_(
                    func.lower(Paper.acl_id) == acl_id,
                    stored_doi == f"{ACL_DOI_PREFIX}{acl_id}",
                )
            )
        ).first()
        if existing:
            return existing

    title = candidate.get("title")
    year = candidate.get("year")
    if title and year is not None:
        normalized_candidate = normalize_title(title)
        tolerance = (
            1 if len(_title_words(title)) >= MIN_TITLE_WORDS_FOR_YEAR_TOLERANCE else 0
        )
        candidate_authors = _author_keys(candidate.get("authors"))
        papers = session.scalars(
            select(Paper)
            .where(Paper.year.between(year - tolerance, year + tolerance))
            .order_by(func.abs(Paper.year - year), Paper.id)
        )
        for paper in papers:
            if not paper.title or normalize_title(paper.title) != normalized_candidate:
                continue
            # A title shared across years (a yearly shared-task overview) is the
            # same paper only when an author corroborates it.
            if paper.year != year and not (
                candidate_authors & _author_keys(paper.authors)
            ):
                continue
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

    # has_full_text/pdf_path are handled separately (not by the generic loop
    # above) because they're a coupled pair, not independent present-over-None
    # fields: pdf_path must always come from whichever side actually has
    # has_full_text=True, so the two can't be merged independently.
    if getattr(existing, "has_full_text", False):
        result["has_full_text"] = True
        result["pdf_path"] = existing.pdf_path
    elif candidate.get("has_full_text"):
        result["has_full_text"] = True
        result["pdf_path"] = candidate.get("pdf_path")

    return result
