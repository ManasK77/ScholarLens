"""Tests for the ACL Anthology ingestion source (Plan 01-04).

Per D-15 (corrected): direct `bibtexparser` parsing of the Anthology's
`anthology+abstracts.bib.gz` export, filtered by topic keyword, with the
Anthology ID/PDF URL derived from the record's `url` field (Pitfall 3:
trailing-slash strip before appending `.pdf`), falling back to the BibTeX
cite key (`entry["ID"]`) for old-style entries with no `url` field
(RESEARCH.md Open Question #1).

No test makes a real network call to aclanthology.org (D-23) — search_topic
tests read the local offline fixture, and download_anthology_export tests
monkeypatch httpx.get.
"""

import gzip

from app.ingestion.sources import acl_anthology

FIXTURE_PATH = "tests/fixtures/acl_anthology_sample.bib"


def test_search_topic_filters_by_keyword_case_insensitive():
    out = acl_anthology.search_topic(FIXTURE_PATH, "multilingual")

    ids = {c["acl_id"] for c in out}
    # Substring match is case-insensitive and doesn't parse semantics, so entries
    # containing "multilingual" inside a negation ("non-multilingual", "excluding
    # multilingual...") correctly match too — this is a plain substring filter,
    # not an NLP classifier.
    assert ids == {
        "2023.acl-long.310",
        "2026.wildre-1.1",
        "2020.coling-main.88",
        "E91-1001",
        "P19-1042",
        "2022.cl-1.5",
    }

    for candidate in out:
        assert set(candidate.keys()) >= {
            "title",
            "authors",
            "year",
            "venue",
            "doi",
            "abstract",
            "acl_id",
            "pdf_url",
        }

    multi_author = next(c for c in out if c["acl_id"] == "2023.acl-long.310")
    assert multi_author["authors"] == ["Kranti, Vajjala", "Doe, Jane"]
    assert multi_author["year"] == 2023
    assert multi_author["venue"] == (
        "Proceedings of the 61st Annual Meeting of the Association for "
        "Computational Linguistics"
    )
    assert multi_author["doi"] == "10.18653/v1/2023.acl-long.310"


def test_acl_id_and_pdf_url_derived_from_url_no_double_slash():
    out = acl_anthology.search_topic(FIXTURE_PATH, "semantic role labeling")

    assert len(out) == 1
    candidate = out[0]
    assert candidate["acl_id"] == "2023.acl-long.310"
    assert candidate["pdf_url"] == "https://aclanthology.org/2023.acl-long.310.pdf"
    assert "//.pdf" not in candidate["pdf_url"]
    assert "/.pdf" not in candidate["pdf_url"]


def test_latex_braced_title_parses_without_raising():
    out = acl_anthology.search_topic(FIXTURE_PATH, "indian")

    assert len(out) == 1
    title = out[0]["title"]
    assert isinstance(title, str)
    assert len(title) > 0


def test_search_topic_no_match_returns_empty_list():
    out = acl_anthology.search_topic(FIXTURE_PATH, "quantum-cryptography-nonsense-xyz")

    assert out == []


def test_fetch_pdf_delegates_to_corpus_download_pdf(monkeypatch):
    calls = {}

    def fake_download_pdf(url, dest_path, timeout=30.0):
        calls["url"] = url
        calls["dest_path"] = dest_path
        return True

    monkeypatch.setattr(acl_anthology.corpus, "download_pdf", fake_download_pdf)

    candidate = {
        "acl_id": "2023.acl-long.310",
        "pdf_url": "https://aclanthology.org/2023.acl-long.310.pdf",
    }
    dest = acl_anthology.fetch_pdf(candidate, "/tmp/pdfs")

    assert calls["url"] == "https://aclanthology.org/2023.acl-long.310.pdf"
    assert calls["dest_path"] == "/tmp/pdfs/2023.acl-long.310.pdf"
    assert dest == "/tmp/pdfs/2023.acl-long.310.pdf"


def test_fetch_pdf_returns_none_on_download_failure(monkeypatch):
    monkeypatch.setattr(
        acl_anthology.corpus, "download_pdf", lambda url, dest_path, timeout=30.0: False
    )

    candidate = {
        "acl_id": "2023.acl-long.310",
        "pdf_url": "https://aclanthology.org/2023.acl-long.310.pdf",
    }
    dest = acl_anthology.fetch_pdf(candidate, "/tmp/pdfs")

    assert dest is None


def test_old_style_url_less_entry_falls_back_to_cite_key():
    out = acl_anthology.search_topic(FIXTURE_PATH, "multilingual")

    old = next(c for c in out if c["acl_id"] == "E91-1001")
    assert old["acl_id"] == "E91-1001"
    assert old["pdf_url"] == "https://aclanthology.org/E91-1001.pdf"
    assert old["year"] == 1991


def test_download_anthology_export_fetches_and_gunzips_when_missing(
    monkeypatch, tmp_path
):
    dest_path = tmp_path / "export.bib"
    original_bytes = b"@inproceedings{fake-2026-entry, title = {Fake}, year = {2026} }"
    compressed = gzip.compress(original_bytes)

    calls = {"count": 0}

    class FakeResponse:
        content = compressed

    def fake_get(url, timeout=60.0, transport=None):
        calls["count"] += 1
        calls["url"] = url
        return FakeResponse()

    monkeypatch.setattr(acl_anthology.httpx, "get", fake_get)

    acl_anthology.download_anthology_export(str(dest_path), force=False)

    assert calls["count"] == 1
    assert dest_path.read_bytes() == original_bytes


def test_download_anthology_export_is_noop_when_dest_exists(monkeypatch, tmp_path):
    dest_path = tmp_path / "export.bib"
    dest_path.write_bytes(b"already here")

    calls = {"count": 0}

    def fake_get(url, timeout=60.0, transport=None):
        calls["count"] += 1
        raise AssertionError("httpx.get should not be called on cache hit")

    monkeypatch.setattr(acl_anthology.httpx, "get", fake_get)

    acl_anthology.download_anthology_export(str(dest_path), force=False)

    assert calls["count"] == 0
    assert dest_path.read_bytes() == b"already here"
