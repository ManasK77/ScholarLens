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

import httpx
import pytest

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


def _make_fake_client(get_impl):
    # httpx.get() (module-level shortcut) doesn't accept a `transport` kwarg —
    # only httpx.Client() does — so download_anthology_export uses a
    # context-managed httpx.Client(transport=...); tests fake that.
    class FakeClient:
        def __init__(self, transport=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, timeout=60.0):
            return get_impl(url, timeout)

    return FakeClient


def test_download_anthology_export_fetches_and_gunzips_when_missing(
    monkeypatch, tmp_path
):
    dest_path = tmp_path / "export.bib"
    original_bytes = b"@inproceedings{fake-2026-entry, title = {Fake}, year = {2026} }"
    compressed = gzip.compress(original_bytes)

    calls = {"count": 0}

    class FakeResponse:
        content = compressed

        def raise_for_status(self):
            pass

    def get_impl(url, timeout):
        calls["count"] += 1
        calls["url"] = url
        return FakeResponse()

    monkeypatch.setattr(acl_anthology.httpx, "Client", _make_fake_client(get_impl))

    acl_anthology.download_anthology_export(str(dest_path), force=False)

    assert calls["count"] == 1
    assert dest_path.read_bytes() == original_bytes


def test_download_anthology_export_raises_clearly_on_non_200(monkeypatch, tmp_path):
    """A bad HTTP response must fail via raise_for_status(), not a confusing
    gzip.decompress error on an HTML/error body (plan-checker follow-up finding)."""
    dest_path = tmp_path / "export.bib"

    class FakeResponse:
        content = b"<html>503 Service Unavailable</html>"

        def raise_for_status(self):
            raise httpx.HTTPStatusError("503", request=None, response=self)

    def get_impl(url, timeout):
        return FakeResponse()

    monkeypatch.setattr(acl_anthology.httpx, "Client", _make_fake_client(get_impl))

    with pytest.raises(httpx.HTTPStatusError):
        acl_anthology.download_anthology_export(str(dest_path), force=False)

    assert not dest_path.exists()


def test_download_anthology_export_is_noop_when_dest_exists(monkeypatch, tmp_path):
    dest_path = tmp_path / "export.bib"
    dest_path.write_bytes(b"already here")

    calls = {"count": 0}

    def get_impl(url, timeout):
        calls["count"] += 1
        raise AssertionError("httpx.Client().get should not be called on cache hit")

    monkeypatch.setattr(acl_anthology.httpx, "Client", _make_fake_client(get_impl))

    acl_anthology.download_anthology_export(str(dest_path), force=False)

    assert calls["count"] == 0
    assert dest_path.read_bytes() == b"already here"


def test_search_topic_only_parses_records_that_could_match(monkeypatch):
    """The 178MB Anthology export must not be parsed in full for one topic.

    `bibtexparser.load` over the real export holds ~3GB and pins a core for
    tens of minutes — and `/collect` pays it on every call, not just the first.
    Records are pre-filtered as raw text so only plausible matches are parsed.
    """
    parsed_inputs = []
    real_loads = acl_anthology.bibtexparser.loads

    def spy_loads(text, *args, **kwargs):
        parsed_inputs.append(text)
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(acl_anthology.bibtexparser, "loads", spy_loads)

    out = acl_anthology.search_topic(FIXTURE_PATH, "semantic role labeling")

    assert len(out) == 1
    assert len(parsed_inputs) == 1
    handed_to_parser = parsed_inputs[0]
    # Only the one plausible record reaches the parser.
    assert handed_to_parser.count("@") >= 1
    assert "2023.acl-long.310" in handed_to_parser
    assert "Abstractive Summarization" not in handed_to_parser


def test_search_topic_prefilter_sees_through_latex_brace_protection():
    """`{I}ndian` must still match the topic `indian`, as full parsing did."""
    out = acl_anthology.search_topic(FIXTURE_PATH, "indian")

    assert [c["acl_id"] for c in out] == ["2026.wildre-1.1"]


def test_search_topic_prefilter_sees_through_wrapped_field_values(tmp_path):
    """A topic split across a wrapped line must not be lost by the prefilter."""
    bib = tmp_path / "wrapped.bib"
    bib.write_text(
        "@inproceedings{wrapped-1,\n"
        '    title = "Cross Lingual\n'
        '             Transfer for Tagging",\n'
        '    author = "Doe, Jane",\n'
        '    year = "2024",\n'
        '    url = "https://aclanthology.org/2024.acl-long.1/",\n'
        '    abstract = "We study cross lingual transfer.",\n'
        "}\n",
        encoding="utf-8",
    )

    out = acl_anthology.search_topic(str(bib), "cross lingual transfer")

    assert [c["acl_id"] for c in out] == ["2024.acl-long.1"]


# ---------------------------------------------------------------------------
# Topic-term matching (D-QUERY-02)
#
# `search_topic` used to require the user's entire topic as one literal
# case-insensitive substring of a title or abstract, so "multilingual prompt
# injection" matched an Anthology paper only if those three words appeared
# adjacent and in that order. The topic is now split by
# `app.ingestion.topic.meaningful_terms` and *every* term must appear — still a
# plain case-insensitive substring test per term, so single-word topics behave
# exactly as before, but the terms no longer have to be adjacent.
# ---------------------------------------------------------------------------


def test_a_multi_word_topic_matches_a_paper_holding_all_its_terms_apart():
    out = acl_anthology.search_topic(
        FIXTURE_PATH, "multilingual semantic role labeling"
    )

    assert [c["acl_id"] for c in out] == ["2023.acl-long.310"]
    # The literal topic string appears nowhere in the matched record — under the
    # old whole-phrase rule this paper was simply invisible to the topic.
    matched = out[0]
    haystack = f"{matched['title']} {matched['abstract']}".lower()
    assert "multilingual semantic role labeling" not in haystack


def test_term_order_and_function_words_do_not_decide_a_match():
    out = acl_anthology.search_topic(
        FIXTURE_PATH, "semantic labeling in multilingual systems"
    )

    assert [c["acl_id"] for c in out] == ["2023.acl-long.310"]


def test_every_term_is_still_required_so_the_search_does_not_broaden():
    """`multilingual` alone matches six fixture records; adding a second term
    the corpus does not carry must return none of them, not all of them."""
    out = acl_anthology.search_topic(FIXTURE_PATH, "multilingual summarization")

    assert out == []


def test_a_single_word_topic_still_matches_as_a_plain_substring():
    """Unchanged behaviour: one term is one substring test, negations included."""
    out = acl_anthology.search_topic(FIXTURE_PATH, "multilingual")

    assert len(out) == 6
