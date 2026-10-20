"""Tests for backend/app/ingestion/dedup.py — the D-19 priority-chain matcher."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ingestion.corpus import Base, insert_paper
from app.ingestion.dedup import find_existing, merge, normalize_title


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _paper_data(**overrides):
    data = {
        "title": "Deep Learning for NLP",
        "authors": ["Alice"],
        "year": 2023,
        "venue": "ACL",
        "doi": None,
        "abstract": "An abstract.",
        "source": "arxiv",
        "pdf_path": None,
        "has_full_text": False,
        "arxiv_id": None,
        "acl_id": None,
    }
    data.update(overrides)
    return data


def test_doi_match_wins_first(session: Session) -> None:
    existing = insert_paper(session, _paper_data(doi="10.1/x", arxiv_id="2301.00001"))
    session.flush()

    candidate = _paper_data(doi="10.1/x", arxiv_id="9999.99999")
    found = find_existing(session, candidate)

    assert found.id == existing.id


def test_arxiv_id_match_when_no_doi(session: Session) -> None:
    existing = insert_paper(session, _paper_data(doi=None, arxiv_id="2301.00001"))
    session.flush()

    candidate = _paper_data(doi=None, arxiv_id="2301.00001")
    found = find_existing(session, candidate)

    assert found.id == existing.id


def test_acl_id_match_when_no_doi_or_arxiv(session: Session) -> None:
    existing = insert_paper(
        session, _paper_data(doi=None, arxiv_id=None, acl_id="2023.acl-long.310")
    )
    session.flush()

    candidate = _paper_data(doi=None, arxiv_id=None, acl_id="2023.acl-long.310")
    found = find_existing(session, candidate)

    assert found.id == existing.id


def test_normalized_title_year_fallback(session: Session) -> None:
    existing = insert_paper(
        session,
        _paper_data(
            doi=None,
            arxiv_id=None,
            acl_id=None,
            title="Deep Learning For NLP",
            year=2023,
        ),
    )
    session.flush()

    candidate = _paper_data(
        doi=None, arxiv_id=None, acl_id=None, title="deep learning for nlp!!", year=2023
    )
    found = find_existing(session, candidate)

    assert found.id == existing.id


def test_no_match_returns_none(session: Session) -> None:
    insert_paper(session, _paper_data(doi="10.1/a"))
    session.flush()

    candidate = _paper_data(
        doi="10.1/completely-different", title="Something Else", year=1999
    )
    found = find_existing(session, candidate)

    assert found is None


def test_normalize_title() -> None:
    assert normalize_title("Deep Learning For NLP") == normalize_title(
        "deep learning for nlp!!"
    )


def test_merge_prefers_present_over_none() -> None:
    existing = type(
        "FakePaper",
        (),
        {"title": None, "doi": "10.1/x", "has_full_text": False, "pdf_path": None},
    )()
    candidate = {
        "title": "New Title",
        "doi": None,
        "has_full_text": False,
        "pdf_path": None,
    }

    result = merge(existing, candidate)

    assert result["title"] == "New Title"
    assert result["doi"] == "10.1/x"


def test_merge_never_overwrites_present_with_none() -> None:
    existing = type(
        "FakePaper",
        (),
        {
            "title": "Existing Title",
            "has_full_text": False,
            "pdf_path": None,
            "doi": None,
        },
    )()
    candidate = {"title": None, "has_full_text": False, "pdf_path": None, "doi": None}

    result = merge(existing, candidate)

    assert result["title"] == "Existing Title"


def test_merge_prefers_full_text_true() -> None:
    existing = type(
        "FakePaper",
        (),
        {"title": None, "doi": None, "has_full_text": True, "pdf_path": "/data/1.pdf"},
    )()
    candidate = {"title": None, "doi": None, "has_full_text": False, "pdf_path": None}

    result = merge(existing, candidate)

    assert result["has_full_text"] is True
    assert result["pdf_path"] == "/data/1.pdf"


# ---------------------------------------------------------------------------
# Cross-source identifiers (S1 audit, 2026-10-10)
#
# Run 1 stored arXiv rows under versioned IDs ("2510.22628v2"), while Semantic
# Scholar supplies arXiv papers' DataCite DOI ("10.48550/arXiv.2510.22628")
# and, until this change, no arXiv ID at all. The same paper also carries a
# preprint year on arXiv and a publication year elsewhere. None of those copies
# was recognised as the paper already stored.
# ---------------------------------------------------------------------------

SENTRA = (
    "Sentra-Guard: A Real-Time Multilingual Defense Against Adversarial LLM Prompts"
)


def _stored(session, **overrides):
    paper = insert_paper(
        session,
        _paper_data(title=SENTRA, year=2025, arxiv_id="2510.22628v2", **overrides),
    )
    session.flush()
    return paper


@pytest.mark.parametrize("arxiv_id", ["2510.22628", "2510.22628v1", "arXiv:2510.22628"])
def test_arxiv_ids_match_regardless_of_version_or_prefix(session, arxiv_id) -> None:
    stored = _stored(session)

    found = find_existing(
        session, _paper_data(title="Another title", year=2025, arxiv_id=arxiv_id)
    )

    assert found is not None and found.id == stored.id


def test_an_arxiv_datacite_doi_matches_the_arxiv_row(session) -> None:
    stored = _stored(session)

    found = find_existing(
        session,
        _paper_data(
            title="Different v2 title", year=2025, doi="10.48550/arXiv.2510.22628"
        ),
    )

    assert found is not None and found.id == stored.id


def test_an_arxiv_id_matches_a_row_that_only_has_the_arxiv_doi(session) -> None:
    stored = insert_paper(
        session,
        _paper_data(
            title="Stored title",
            source="semantic_scholar",
            doi="10.48550/arXiv.2510.22628",
        ),
    )
    session.flush()

    found = find_existing(session, _paper_data(title="X", arxiv_id="2510.22628v3"))

    assert found is not None and found.id == stored.id


def test_an_acl_anthology_doi_matches_the_acl_row(session) -> None:
    stored = insert_paper(session, _paper_data(title="Stored", acl_id="2024.wmt-1.30"))
    session.flush()

    found = find_existing(
        session, _paper_data(title="X", doi="10.18653/v1/2024.wmt-1.30")
    )

    assert found is not None and found.id == stored.id


@pytest.mark.parametrize(
    "doi",
    [
        "10.1109/AIB.2024.42",
        "10.1109/aib.2024.42",
        "https://doi.org/10.1109/AIB.2024.42",
    ],
)
def test_dois_match_case_and_resolver_prefix_insensitively(session, doi) -> None:
    stored = insert_paper(session, _paper_data(doi="10.1109/AIB.2024.42"))
    session.flush()

    found = find_existing(session, _paper_data(title="X", doi=doi))

    assert found is not None and found.id == stored.id


def test_a_long_title_matches_across_adjacent_years(session) -> None:
    """Preprint year on arXiv, publication year at the venue."""
    stored = _stored(session, doi=None)

    found = find_existing(session, _paper_data(title=SENTRA, year=2026))

    assert found is not None and found.id == stored.id


def test_titles_two_years_apart_are_different_papers(session) -> None:
    _stored(session)

    assert find_existing(session, _paper_data(title=SENTRA, year=2027)) is None


def test_a_short_generic_title_still_needs_the_same_year(session) -> None:
    insert_paper(session, _paper_data(title="Deep Learning For NLP", year=2023))
    session.flush()

    assert (
        find_existing(session, _paper_data(title="Deep Learning for NLP", year=2024))
        is None
    )


def test_titles_differing_only_in_dash_spacing_match(session) -> None:
    stored = insert_paper(
        session,
        _paper_data(title="MIPIAD: Defense with Qwen -- TF-IDF Hybrid", year=2026),
    )
    session.flush()

    found = find_existing(
        session,
        _paper_data(title="MIPIAD: Defense with Qwen--TF-IDF Hybrid", year=2026),
    )

    assert found is not None and found.id == stored.id


def test_normalize_title_reads_punctuation_variants_alike() -> None:
    assert normalize_title("Pre-training") == normalize_title("Pretraining")
    assert normalize_title("Don't") == normalize_title("Dont")
    assert normalize_title("Don’t") == normalize_title("Dont")


@pytest.mark.parametrize(
    ("stored_title", "candidate_title"),
    [
        (
            "Pre-training Multilingual Models for Retrieval",
            "Pretraining Multilingual Models for Retrieval",
        ),
        (
            "Why Models Don't Resist Prompt Injection",
            "Why Models Dont Resist Prompt Injection",
        ),
        (
            "Why Models Don’t Resist Prompt Injection",
            "Why Models Don't Resist Prompt Injection",
        ),
        (
            "Multi-lingual Prompt Injection Attacks Revisited",
            "Multilingual Prompt Injection Attacks Revisited",
        ),
    ],
)
def test_titles_differing_only_in_punctuation_match(
    session, stored_title, candidate_title
) -> None:
    stored = insert_paper(session, _paper_data(title=stored_title, year=2024))
    session.flush()

    found = find_existing(session, _paper_data(title=candidate_title, year=2024))

    assert found is not None and found.id == stored.id


def test_distinct_titles_are_not_matched_by_dropping_punctuation(session) -> None:
    insert_paper(session, _paper_data(title="Pre-training Language Models", year=2024))
    session.flush()

    assert (
        find_existing(
            session, _paper_data(title="Pre-trained Language Models", year=2024)
        )
        is None
    )


RECURRING = (
    "Overview of the Shared Task on Multilingual Offensive Language Identification"
)


def test_a_recurring_generic_title_is_not_merged_across_years(session) -> None:
    """Yearly shared-task overviews share a title and are different papers."""
    insert_paper(
        session,
        _paper_data(title=RECURRING, year=2023, authors=["Dana Lee", "Omar Ali"]),
    )
    session.flush()

    found = find_existing(
        session,
        _paper_data(title=RECURRING, year=2024, authors=["Priya Rao", "Sam Cole"]),
    )

    assert found is None


def test_a_recurring_generic_title_still_matches_within_a_year(session) -> None:
    stored = insert_paper(
        session, _paper_data(title=RECURRING, year=2023, authors=["Dana Lee"])
    )
    session.flush()

    found = find_existing(
        session, _paper_data(title=RECURRING, year=2023, authors=["Priya Rao"])
    )

    assert found is not None and found.id == stored.id


def test_the_same_authors_still_match_a_title_across_adjacent_years(session) -> None:
    stored = insert_paper(
        session,
        _paper_data(title=RECURRING, year=2023, authors=["Dana Lee", "Omar Ali"]),
    )
    session.flush()

    found = find_existing(
        session,
        _paper_data(title=RECURRING, year=2024, authors=["D. Lee", "Omar Ali"]),
    )

    assert found is not None and found.id == stored.id


def test_a_cross_year_title_match_without_authors_is_not_trusted(session) -> None:
    insert_paper(session, _paper_data(title=RECURRING, year=2023, authors=["Dana Lee"]))
    session.flush()

    assert (
        find_existing(session, _paper_data(title=RECURRING, year=2024, authors=[]))
        is None
    )


def test_a_stored_doi_with_a_resolver_prefix_matches_a_bare_doi(session) -> None:
    stored = insert_paper(
        session, _paper_data(title="Stored", doi="https://doi.org/10.1234/ABC.1")
    )
    session.flush()

    found = find_existing(session, _paper_data(title="Other", doi="10.1234/abc.1"))

    assert found is not None and found.id == stored.id


def test_a_stored_doi_with_a_doi_scheme_matches_a_resolver_url(session) -> None:
    stored = insert_paper(session, _paper_data(title="Stored", doi="doi:10.1234/abc.1"))
    session.flush()

    found = find_existing(
        session, _paper_data(title="Other", doi="https://dx.doi.org/10.1234/abc.1")
    )

    assert found is not None and found.id == stored.id


@pytest.mark.parametrize(
    ("stored_id", "candidate_id"),
    [
        ("hep-th/9901001v1", "hep-th/9901001"),
        ("hep-th/9901001", "arXiv:hep-th/9901001v3"),
        ("math.GT/0309136v2", "math.gt/0309136"),
    ],
)
def test_old_style_arxiv_ids_match_across_version_and_case(
    session, stored_id, candidate_id
) -> None:
    stored = insert_paper(session, _paper_data(title="Stored", arxiv_id=stored_id))
    session.flush()

    found = find_existing(session, _paper_data(title="Other", arxiv_id=candidate_id))

    assert found is not None and found.id == stored.id


def test_different_old_style_arxiv_ids_do_not_match(session) -> None:
    insert_paper(session, _paper_data(title="Stored", arxiv_id="hep-th/9901001v1"))
    session.flush()

    assert (
        find_existing(session, _paper_data(title="Other", arxiv_id="hep-th/9901002"))
        is None
    )
    assert (
        find_existing(session, _paper_data(title="Other", arxiv_id="hep-th/990100"))
        is None
    )


def test_an_arxiv_id_prefix_of_a_longer_id_does_not_match(session) -> None:
    insert_paper(session, _paper_data(title="Stored", arxiv_id="2510.226281"))
    session.flush()

    assert (
        find_existing(session, _paper_data(title="Other", arxiv_id="2510.22628"))
        is None
    )
